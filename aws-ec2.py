#!/usr/bin/env python

'''
 TODO:
    * Implement RDS
    * Implement Elasticache and Route53 if/when needed

EC2 external inventory script
=================================

Generates inventory that Ansible formatted inventory by making API request to
AWS EC2 using the Boto3 library.

NOTE: While this script allows you to pass AWS credentials to it through a config
YAML, it is highly recommended to use seporate boto3 profiles or and IAM role if
running from within AWS

This script tries to find a config file name aws-ec2.yml alongside it. If this
file is not found the DEFAULTS dict WILL BE USED. To specify a different path
to aws-ec2.yml, pass the path to the --config-file opetion or define the
EC2_YML_PATH environment variable:

    export EC2_YML_PATH=/path/to/my_ec2.yml

The script sets the dictionary returned by boto3 as the hostvars for each host.

Check the aws-ec2.yml file for more information about settings passed to 
the script
'''
# (c) 2012, Peter Sankauskas
#
# Rewritten for python3 and boto3:
# (c) 2020, William Horowitz
#
# This file is part of Ansible,
#
# Ansible is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# Ansible is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with Ansible.  If not, see <http://www.gnu.org/licenses/>.

######################################################################

import argparse
import boto3
from collections import defaultdict
from copy import deepcopy
from datetime import date, datetime
import json
import os
import re
import sys
from time import time
import yaml

if sys.version_info[0] < 3:
    import urllib2
else:
    import urllib.request


DEFAULTS = {
    'ec2': {
        'regions': ['us-east-1', 'us-west-1'],
        'destination_variable': 'PrivateDnsName',
        'vpc_destination_variable': 'PrivateIpAddress',
        'route53': False,
        'rds': False,
        'elasticache': False,
        'all_instances': False,
        'instance_states': ['running'],
        'all_elasticache_replication_group': False,
        'all_elasticache_cluster': False,
        'all_elasticache_nodes': False,
        'enable_caching': False,
        'cache_path': '~/.ansible/tmp',
        'cache_max_age': 300,
        'nested_groups': True,
        'replace_dash_in_groups': True,
        'group_by_instance_id': False,
        'group_by_region': True,
        'group_by_availability_zone': True,
        'roup_by_aws_account': True,
        'group_by_ami_id': True,
        'group_by_instance_type': True,
        'group_by_instance_state': False,
        'group_by_platform': True,
        'group_by_key_pair': False,
        'group_by_vpc_id': True,
        'group_by_security_group': False,
        'group_by_tag_keys': True,
        'group_by_tag_none': True,
        'group_by_route53_names': False,
        'group_by_rds_engine': False,
        'group_by_rds_parameter_group': False,
        'group_by_elasticache_engine': False,
        'group_by_elasticache_cluster': False,
        'group_by_elasticache_parameter_group': False,
        'group_by_elasticache_replication_group': False
    }
}

class Ec2Inventory(object):

    def _empty_inventory(self):
        return {"_meta": {"hostvars": {}}}


    def _json_serial(self, obj):
        """JSON serializer for objects not serializable by default json code"""
        if isinstance(obj, (datetime, date)):
            return obj.isoformat()
        raise TypeError("Type %s not serializable" % type(obj))


    def __init__(self):
        '''Primary path of execution'''
        # Dict representing the inventory
        self.inventory = self._empty_inventory()

        # Index of hostname (address) to region and instance ID
        self.index = {}

        # Account (OwnerID) of the reservations to be processed in this inventory
        self.aws_account_id = None

        # Index of hostname (address) to instance ID
        self.index = {}

        # Boto profile to use (if any)
        self.boto_profile = None

        # AWS credentials.
        self.credentials = {}

        # Parse CLI args. Not supporting settings file yet
        self.parse_cli_args()
        self.read_settings()

        # Update Inventory
        self.update_inventory()

        # Data to print
        if self.args.host:
            data_to_print = self.get_host_info()
        elif self.args.list:
            # Display list of instances for inventory
            if self.inventory == self._empty_inventory() and self.settings['enable_caching']:
                data_to_print = self.get_inventory_from_cache()
            else:
                data_to_print = self.json_format_dict(self.inventory, True)

        print(data_to_print)


    def parse_cli_args(self):
        ''' Command line argument processing '''
        parser = argparse.ArgumentParser(description='Produce an Ansible Inventory file based on EC2')
        parser.add_argument('--list', action='store_true', default=True,
                            help='List instances (default: True)')
        parser.add_argument('--host', action='store',
                            help='Get all the variables about a specific instance')
        parser.add_argument('--refresh-cache', action='store_true', default=False,
                            help='Force refresh of cache by making API requests to EC2 (default: False - use cache files)')
        parser.add_argument('--profile', '--boto-profile', action='store', dest='boto_profile',
                            help='Use boto profile for connections to EC2')
        parser.add_argument('--config-file', action='store', dest='config_file',
                            help='Config file to use for settings and credentials')
        parser.add_argument('--yaml', action='store_true', default=False,
                            help='Output inventory in JSON format instead of YAML')
        self.args = parser.parse_args()


    def read_settings(self):
        ''' Reads the settings from the aws-ec2.yml file '''

        # Prefer config-file specified at command line, then prefer environment variables,
        # then file in same directory as the script. If none of the exist or can be parsed,
        # DEFAULTS dict is used for config. The DEFAULTS values are merged with config
        # file so that config file variables take precedence.

        self.config = DEFAULTS

        if self.args.config_file:
            config_file = os.path.abspath(self.args.config_file)
        elif os.environ.get('EC2_YML_PATH'):
            config_file = os.environ.get('EC2_YML_PATH')
            config_file = os.path.abspath(config_file)
        else:
            config_file = os.path.abspath(__file__)
            config_file = config_file.replace('.py', '.yml')

        if os.path.exists(config_file) and config_file.endswith('.yml'):
            try:
                with open(config_file, 'r') as stream:
                    config_from_file = yaml.safe_load(stream)
                    self.config.update(config_from_file)
            except yaml.YAMLError as exc:
                print("Failed to find parse file: %s" % config_file)
                print(exc)
                
        # Create new dict for just ec2 config
        self.settings = self.config['ec2']

        # Instance states to be gathered in inventory. Default is 'running'.
        # Setting 'all_instances' to 'yes' overrides this option.
        ec2_valid_instance_states = [
            'pending',
            'running',
            'shutting-down',
            'terminated',
            'stopping',
            'stopped'
        ]
        if 'all_instances' in self.settings and self.settings['all_instances']:
            self.settings['instance_states'] = ec2_valid_instance_states
        elif not 'instance_states' in self.settings:
            self.settings['instance_states'] = ['running']

        # boto3 configuration profile (prefer CLI argument then environment variables then config file)
        # Note that if running from within AWS, recommend leaving out credentials and using IAM roles
        self.boto_profile = self.args.boto_profile
        if not self.boto_profile and 'boto_profile' in self.settings:
            self.boto_profile = self.settings['boto_profile']

        # AWS credentials (prefer environment variables)
        if not (self.boto_profile or os.environ.get('AWS_ACCESS_KEY_ID') or
                os.environ.get('AWS_PROFILE')):
            if 'credentials' in self.config and self.config['credentials'] is not None:
                if 'aws_access_key_id' in self.config['credentials']:
                    self.credentials = {
                        'aws_access_key_id': self.config['credentials']['aws_access_key_id'],
                        'aws_secret_access_key': self.config['credentials']['aws_secret_access_key']
                    }
                    if 'aws_security_token' in self.config['credentials']:
                        self.credentials['security_token'] = self.config['credentials']['aws_security_token']

        # Cache related
        if self.settings['enable_caching']:
            cache_dir = os.path.expanduser(self.settings['cache_path'])
            if self.boto_profile:
                cache_dir = os.path.join(cache_dir, 'profile_' + self.boto_profile)
            if not os.path.exists(cache_dir):
                os.makedirs(cache_dir)
            
            cache_name = 'ansible-ec2'
            cache_id = self.boto_profile or os.environ.get('AWS_ACCESS_KEY_ID', self.credentials.get('aws_access_key_id'))
            if cache_id:
                cache_name = '%s-%s' % (cache_name, cache_id)
            cache_name += '-' + str(abs(hash(__file__)))[1:7]
            self.cache_path_cache = os.path.join(cache_dir, "%s.cache" % cache_name)
            self.cache_path_index = os.path.join(cache_dir, "%s.index" % cache_name)


    def is_cache_valid(self):
        ''' Determines if the cache files have expired, or if it is still valid '''

        if os.path.isfile(self.cache_path_cache):
            mod_time = os.path.getmtime(self.cache_path_cache)
            current_time = time()
            if (mod_time + self.settings['cache_max_age']) > current_time:
                if os.path.isfile(self.cache_path_index):
                    return True

        return False


    def update_inventory(self):
        ''' Do API calls to each region, and save data in cache files '''
        for region in self.settings['regions']:
            self.get_instances(region)

        if self.settings['enable_caching']:
            if self.args.refresh_cache or not self.is_cache_valid():
                self.write_to_cache(self.inventory, self.cache_path_cache)
                self.write_to_cache(self.index, self.cache_path_index)


    def get_aws_connection(self, aws_service, region="us-east-1"):
        ''' Get an AWS connection with a region. Defaults to default boto3
            region if none is specified'''
        # Use Credentials (and optional token) if provided
        if self.credentials:
            if 'aws_session_token' in self.credentials:
                return boto3.client(
                    aws_service,
                    aws_access_key_id=self.credentials['aws_access_key_id'],
                    aws_secret_access_key=self.credentials['aws_secret_access_key'],
                    aws_session_token=self.credentials['aws_session_token'],
                    region_name=region
                )
            return boto3.client(
                aws_service,
                aws_access_key_id=self.credentials['aws_access_key_id'],
                aws_secret_access_key=self.credentials['aws_secret_access_key'],
                region_name=region
            )
        
        # Use boto3 profile if specified 
        elif self.boto_profile:
            session = boto3.Session(profile_name=self.boto_profile)
            return session.client(aws_service, region_name=region)

        # STS assume role if specified (assumes existing IAM role with permissions to assume role)
        elif 'iam_assume_role' in self.settings:
            this_instance_az = urllib2.urlopen('http://169.254.169.254/latest/meta-data/placement/availability-zone').read()
            this_instance_az = urllib.request.urlopen('http://169.254.169.254/latest/meta-data/placement/availability-zone').read().decode()
            sts_client = boto3.client('sts', region_name=this_instance_az[0:-1])
            assumed_role = sts_client.assume_role(
                RoleArn = self.settings['iam_assume_role'],
                RoleSessionName = 'ansible-dyInv'
            )
            return boto3.client(
                aws_service,
                aws_access_key_id=assumed_role['Credentials']['AccessKeyId'],
                aws_secret_access_key=assumed_role['Credentials']['SecretAccessKey'],
                aws_session_token=assumed_role['Credentials']['SessionToken'],
                region_name=region
            )

        # Otherwise, use defaults (such as ec2 IAM role or default boto3 config)
        else:
            return boto3.client(aws_service, region_name=region)


    def get_instances(self, region):
        ''' Makes an AWS EC2 API call to the list of instances in a particular region '''

        conn = self.get_aws_connection('ec2', region)
        reservations = []
        
        # Filtering is not supported at this time
        if 'instance_filters' in self.settings:
            response = conn.describe_instances(Filters=self.settings['instance_filters'])
        else:
            response = conn.describe_instances()
        
        reservations = response['Reservations']

        if (not self.aws_account_id) and reservations:
            self.aws_account_id = reservations[0]['OwnerId']
        
        for reservation in reservations:
            for instance in reservation['Instances']:
                self.add_instance(instance, region)


    def add_instance(self, instance, region):
        ''' Adds an instance to the inventory and index, as long as it is
        addressable '''

        # Only return instances with desired instance states
        if instance['State']['Name'] not in self.settings['instance_states']:
            return

        # Select the best destination address
        # When destination_format and destination_format_tags are specified
        # the following code will attempt to find the instance tags first,
        # then the instance attributes next, and finally if neither are found
        # assign nil for the desired destination format attribute.
        if 'destination_format' in self.settings and 'destination_format_tags' in self.settings:
            dest_vars = []
            inst_tags = instance['Tags']
            for tag in self.settings['destination_format_tags']:
                if tag in inst_tags:
                    dest_vars.append(inst_tags[tag])
                elif hasattr(instance, tag):
                    dest_vars.append(getattr(instance, tag))
                else:
                    dest_vars.append('nil')

            dest = self.settings['destination_format'].format(*dest_vars)
        elif instance['SubnetId']:
            # Try using a VPC instance variable (like PrivateIpAddress)
            # If it is not found, try to find it in a tag
            if 'vpc_destination_variable' in self.settings:
                dest = instance.get(self.settings['vpc_destination_variable'])
                if instance.get('Tags'):
                    for itags in instance.get('Tags'):
                        if itags.get('Key') == self.settings['vpc_destination_variable']:
                            dest = itags['Value']
                            break
        else:
            if 'destination_variable' in self.settings:
                dest = instance.get(self.settings['destination_variable'])
                if instance.get('Tags'):
                    for itags in instance.get('Tags'):
                        if itags.get('Key') == self.settings['destination_variable']:
                            dest = itags['Value']
                            break

        # Skip instances we cannot address (e.g. private VPC subnet)
        if not dest:
            return

        # Set the inventory name
        hostname = None
        if 'hostname_variable' in self.settings:
            if self.settings['hostname_variable'].startswith('tag_'):
                if instance.get('Tags'):
                    for itags in instance.get('Tags'):
                        if itags.get('Key') == self.settings['hostname_variable'][4:]:
                            hostname = itags['Value']
                            break
            else:
                hostname = instance.get(self.settings['hostname_variable'])

        # set the hostname from route53
        '''
        if self.settings['route53_enabled'] and self.settings['route53_hostnames']:
            route53_names = self.get_instance_route53_names(instance)
            for name in route53_names:
                if name.endswith(self.settings['route53_hostnames']):
                    hostname = name
        '''

        # If we can't get a nice hostname, use the destination address
        if not hostname:
            hostname = dest
        # to_safe strips hostname characters like dots, so don't strip route53 hostnames
        elif 'route53_enabled' in self.settings and \
            self.settings['route53_enable'] and \
            'route53_hostnames' in self.settings and \
            hostname.endswith(self.settings['route53_hostnames']):
            hostname = hostname.lower()
        else:
            hostname = self.to_safe(hostname).lower()

        # if we only want to include hosts that match a pattern, skip those that don't
        if 'pattern_include' in self.settings and not re.search(self.settings['pattern_include'], hostname):
            return

        # if we need to exclude hosts that match a pattern, skip those
        if 'pattern_exclude' in self.settings and re.search(self.settings['pattern_exclude'], hostname):
            return

        # Add to index
        self.index[hostname] = [region, instance['InstanceId']]

        # Inventory: Group by instance ID (always a group of 1)
        if 'group_by_instance_id' in self.settings and self.settings['group_by_instance_id']:
            self.inventory[instance['InstanceId']]['hosts'] = [hostname]
            if 'nested_groups' in self.settings and self.settings['nested_groups']:
                self.push_group(self.inventory, 'instances', instance['InstanceId'])

        # Inventory: Group by region
        if 'group_by_region' in self.settings and self.settings['group_by_region']:
            self.push(self.inventory, self.to_safe(region), hostname)
            if 'nested_groups' in self.settings and self.settings['nested_groups']:
                self.push_group(self.inventory, 'regions', self.to_safe(region))

        # Inventory: Group by availability zone
        if 'group_by_availability_zone' in self.settings and self.settings['group_by_availability_zone']:
            az = self.to_safe(instance['Placement']['AvailabilityZone'])
            self.push(self.inventory, az, hostname)
            if 'nested_groups' in self.settings and self.settings['nested_groups']:
                if self.settings['group_by_region']:
                    self.push_group(self.inventory, self.to_safe(region), az)
                self.push_group(self.inventory, 'zones', az)

        # Inventory: Group by Amazon Machine Image (AMI) ID
        if 'group_by_ami_id' in self.settings and self.settings['group_by_ami_id']:
            ami_id = self.to_safe(instance['ImageId'])
            self.push(self.inventory, ami_id, hostname)
            if 'nested_groups' in self.settings and self.settings['nested_groups']:
                self.push_group(self.inventory, 'images', ami_id)

        # Inventory: Group by instance type
        if 'group_by_instance_type' in self.settings and self.settings['group_by_instance_type']:
            type_name = self.to_safe('type_' + instance['InstanceType'])
            self.push(self.inventory, type_name, hostname)
            if 'nested_groups' in self.settings and self.settings['nested_groups']:
                self.push_group(self.inventory, 'types', type_name)

        # Inventory: Group by instance state
        if 'group_by_instance_state' in self.settings and self.settings['group_by_instance_state']:
            state_name = self.to_safe('instance_state_' + instance['State']['Name'])
            self.push(self.inventory, state_name, hostname)
            if 'nested_groups' in self.settings and self.settings['nested_groups']:
                self.push_group(self.inventory, 'instance_states', state_name)

        # Inventory: Group by platform
        if 'group_by_platform' in self.settings and self.settings['group_by_platform']:
            if instance.get('Platform'):
                platform = self.to_safe('platform_' + instance['Platform'])
            else:
                platform = self.to_safe('platform_undefined')
            self.push(self.inventory, platform, hostname)
            if 'nested_groups' in self.settings and self.settings['nested_groups']:
                self.push_group(self.inventory, 'platforms', platform)

        # Inventory: Group by key pair
        if 'group_by_key_pair' in self.settings and self.settings['group_by_key_pair']:
            if instance.get('KeyName'):
                key_name = self.to_safe('key_' + instance['KeyName'])
                self.push(self.inventory, key_name, hostname)
                if 'nested_groups' in self.settings and self.settings['nested_groups']:
                    self.push_group(self.inventory, 'keys', key_name)

        # Inventory: Group by VPC
        if 'group_by_vpc_id' in self.settings and self.settings['group_by_vpc_id']:
            if instance.get('VpcId'):
                vpc_id_name = self.to_safe('vpc_id_' + instance['VpcId'])
                self.push(self.inventory, vpc_id_name, hostname)
                if 'nested_groups' in self.settings and self.settings['nested_groups']:
                    self.push_group(self.inventory, 'vpcs', vpc_id_name)

        # Inventory: Group by security group
        if 'group_by_security_group' in self.settings and self.settings['group_by_security_group']:
            try:
                for group in instance.get('SecurityGroups'):
                    key = self.to_safe("security_group_" + group.get('GroupName'))
                    self.push(self.inventory, key, hostname)
                    if 'nested_groups' in self.settings and self.settings['nested_groups']:
                        self.push_group(self.inventory, 'security_groups', key)
            except AttributeError:
                self.fail_with_error('\n'.join(['Package boto seems a bit older.',
                                                'Please upgrade boto >= 2.3.0.']))

        # Inventory: Group by AWS account ID
        if 'group_by_aws_account' in self.settings and self.settings['group_by_aws_account']:
            self.push(self.inventory, self.aws_account_id, hostname)
            if 'nested_groups' in self.settings and self.settings['nested_groups']:
                self.push_group(self.inventory, 'accounts', self.aws_account_id)

        # Inventory: Group by tag keys
        if 'group_by_tag_keys' in self.settings and self.settings['group_by_tag_keys']:
            if instance.get('Tags'):
                for itag in instance.get('Tags'):
                    key = self.to_safe("tag_" + itag['Key'] + "=" + itag['Value'])
                    self.push(self.inventory, key, hostname)
                    if 'nested_groups' in self.settings and self.settings['nested_groups']:
                        self.push_group(self.inventory, 'tags', self.to_safe("tag_" + key))

        '''
        # Inventory: Group by Route53 domain names if enabled
        if self.route53_enabled and self.settings['group_by_route53_names']:
            route53_names = self.get_instance_route53_names(instance)
            for name in route53_names:
                self.push(self.inventory, name, hostname)
                if 'nested_groups' in self.settings and self.settings['nested_groups']:
                    self.push_group(self.inventory, 'route53', name)
        '''

        # Global Tag: instances without tags
        if 'group_by_tag_none' in self.settings and self.settings['group_by_tag_none']:
            if len(instance.get('Tags')) is None:
                self.push(self.inventory, 'tag_none', hostname)
                if 'nested_groups' in self.settings and self.settings['nested_groups']:
                    self.push_group(self.inventory, 'tags', 'tag_none')

        # Global Tag: tag all EC2 instances
        self.push(self.inventory, 'ec2', hostname)

        # Set full dict returned by describe_instances() as hostvars for host
        self.inventory["_meta"]["hostvars"][hostname] = instance
        self.inventory["_meta"]["hostvars"][hostname]['ansible_host'] = dest


    def fail_with_error(self, err_msg, err_operation=None):
        '''log an error to std err for ansible-playbook to consume and exit'''
        if err_operation:
            err_msg = 'ERROR: "{err_msg}", while: {err_operation}'.format(
                err_msg=err_msg, err_operation=err_operation)
        sys.stderr.write(err_msg)
        sys.exit(1)


    def to_safe(self, word):
        ''' Converts 'bad' characters in a string to underscores so they comply 
        with ansible naming conventions '''
        word = word.lower()
        regex = r"[^A-Za-z0-9\_"
        if not self.settings['replace_dash_in_groups']:
            regex += r"\-"
        return re.sub(regex + "]", "_", word)

    
    def push(self, my_dict, key, element):
        ''' Push an element onto a host list '''
        if not key in my_dict:
            my_dict[key] = {'hosts': [], 'vars':{}, 'children':[]}
        my_dict[key]['hosts'].append(element)

    def push_group(self, my_dict, key, element):
        ''' Push a group as a child of another group. '''
        if not key in my_dict:
            my_dict[key] = {'hosts': [], 'vars':{}, 'children':[]}
        if element not in my_dict[key]['children']:
            my_dict[key]['children'].append(element)


    def get_host_info(self):
        ''' Get variables about a specific host '''
        if len(self.index) == 0:
            # Need to load index from cache
            self.load_index_from_cache()

        if self.args.host not in self.index:
            # try updating the cache
            self.do_api_calls_update_cache()
            if self.args.host not in self.index:
                # host might not exist anymore
                return self.json_format_dict({}, True)

        (region, instance_id) = self.index[self.args.host]

        instance = self.get_instance(region, instance_id)
        return self.json_format_dict(instance, True)


    def get_instance(self, region, instance_id):
        ''' Get specific instanc(s) given a list of instance Ids '''
        conn = self.get_aws_connection('ec2', region)
        response = conn.describe_instances(InstanceIds=[instance_id])
        for reservation in response['Reservations']:
            for instance in reservation['Instances']:
                return instance

    
    def is_cache_valid(self):
        ''' Determines if the cache files have expired, or if it is still valid '''

        if os.path.isfile(self.cache_path_cache):
            mod_time = os.path.getmtime(self.cache_path_cache)
            current_time = time()
            if (mod_time + self.settings['cache_max_age']) > current_time:
                if os.path.isfile(self.cache_path_index):
                    return True

        return False

    def load_index_from_cache(self):
        ''' Reads the index from the cache file sets self.index '''
        with open(self.cache_path_index, 'rb') as f:
            self.index = json.load(f)


    def get_inventory_from_cache(self):
        ''' Reads the inventory from the cache file and returns it as a JSON
        object '''
        with open(self.cache_path_cache, 'r') as f:
            try:
                yaml_inventory = yaml.safe_load(f)
                return yaml_inventory
            except:
                json_inventory = f.read()
                return json_inventory    

    def write_to_cache(self, data, filename):
        ''' Writes data in JSON format to a file '''
        json_data = self.json_format_dict(data, True)
        with open(filename, 'w+') as f:
            f.write(json_data)


    def json_format_dict(self, data, pretty=False):
        ''' Converts a dict to a JSON object and dumps it as a formatted
        string '''
        if self.args.yaml:
            return yaml.dump(data)
        elif pretty:
            return json.dumps(data, sort_keys=True, indent=2, default=self._json_serial)
        else:
            return json.dumps(data, default=self._json_serial)


if __name__ == '__main__':
    Ec2Inventory()

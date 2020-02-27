# Ansible-AWS-EC2
Dynamic Inventory For Ansible from AWS EC2

## aws-ec2.py
Dynamic inventory for Ansible Tower. Supports the required functionality for an Ansible Dyanmic Inventory script shown [here][1]. The following options are supported by the script:

* `--list` - Contents for the inventory including hostvars. Needed to be compatiable with Ansible
* `--host` - Get information on a specific instance in the inventory
* `--refresh-cache` - If `enable_caching` is set to true, this will force a cache file locally stored on the file system to be update. Not necessary if using an external cache such as redis configured in the `ansible.cfg` file
* `--profile` - Specify a boto3 profile to use. If you have boto3 configured, the default will be used
* `--config-file` - Specifiy a config file to use. By default, the script looks for a file of the same name but ending in `.yml` as the config file. This is overridden by the environment variable `EC2_YML_PATH` which is in turn overridden by this option
* `--yaml` - Output your inventory as a yaml

For more details about the configuation of the script, please check the `aws-ec2.yml` file.


[1]: https://docs.ansible.com/ansible/latest/dev_guide/developing_inventory.html

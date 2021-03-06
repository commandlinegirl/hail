import os
import logging
import base64
import json

from hailtop import aiogoogle

from ..batch_configuration import PROJECT, DEFAULT_NAMESPACE
from ..worker_config import WorkerConfig
from ..log_store import LogStore

log = logging.getLogger('create_instance')

BATCH_WORKER_IMAGE = os.environ['HAIL_BATCH_WORKER_IMAGE']

log.info(f'BATCH_WORKER_IMAGE {BATCH_WORKER_IMAGE}')


async def create_instance(app, zone, machine_name, machine_type, activation_token,
                          max_idle_time_msecs, worker_local_ssd_data_disk,
                          worker_pd_ssd_data_disk_size_gb, boot_disk_size_gb):
    log_store: LogStore = app['log_store']
    compute_client: aiogoogle.ComputeClient = app['compute_client']

    if worker_local_ssd_data_disk:
        worker_data_disk = {
            'type': 'SCRATCH',
            'autoDelete': True,
            'interface': 'NVME',
            'initializeParams': {
                'diskType': f'zones/{zone}/diskTypes/local-ssd'
            }}
        worker_data_disk_name = 'nvme0n1'
    else:
        worker_data_disk = {
            'autoDelete': True,
            'initializeParams': {
                'diskType': f'projects/{PROJECT}/zones/{zone}/diskTypes/pd-ssd',
                'diskSizeGb': str(worker_pd_ssd_data_disk_size_gb)
            }
        }
        worker_data_disk_name = 'sdb'

    config = {
        'name': machine_name,
        'machineType': f'projects/{PROJECT}/zones/{zone}/machineTypes/{machine_type}',
        'labels': {
            'role': 'batch2-agent',
            'namespace': DEFAULT_NAMESPACE
        },

        'disks': [{
            'boot': True,
            'autoDelete': True,
            'initializeParams': {
                'sourceImage': f'projects/{PROJECT}/global/images/batch-worker-12',
                'diskType': f'projects/{PROJECT}/zones/{zone}/diskTypes/pd-ssd',
                'diskSizeGb': str(boot_disk_size_gb)
            }
        }, worker_data_disk],

        'networkInterfaces': [{
            'network': 'global/networks/default',
            'networkTier': 'PREMIUM',
            'accessConfigs': [{
                'type': 'ONE_TO_ONE_NAT',
                'name': 'external-nat'
            }]
        }],

        'scheduling': {
            'automaticRestart': False,
            'onHostMaintenance': "TERMINATE",
            'preemptible': True
        },

        'serviceAccounts': [{
            'email': f'batch2-agent@{PROJECT}.iam.gserviceaccount.com',
            'scopes': [
                'https://www.googleapis.com/auth/cloud-platform'
            ]
        }],

        'metadata': {
            'items': [{
                'key': 'startup-script',
                'value': '''
#!/bin/bash
set -x

curl -s -H "Metadata-Flavor: Google" "http://metadata.google.internal/computeMetadata/v1/instance/attributes/run_script"  >./run.sh

nohup /bin/bash run.sh >run.log 2>&1 &
    '''
            }, {
                'key': 'run_script',
                'value': rf'''
#!/bin/bash
set -x

# only allow udp/53 (dns) to metadata server
# -I inserts at the head of the chain, so the ACCEPT rule runs first
iptables -I DOCKER-USER -d 169.254.169.254 -j DROP
iptables -I DOCKER-USER -d 169.254.169.254 -p udp -m udp --destination-port 53 -j ACCEPT

docker network create public --opt com.docker.network.bridge.name=public
docker network create private --opt com.docker.network.bridge.name=private
# make other docker containers not route-able
iptables -I DOCKER-USER -i public -d 172.16.0.0/12 -j DROP
# not used, but ban it anyway!
iptables -I DOCKER-USER -i public -d 192.168.0.0/16 -j DROP

# create network for batch-worker that allows metadata server
docker network create batch-worker --opt com.docker.network.bridge.name=batch-worker
iptables -I DOCKER-USER -i batch-worker -d 169.254.169.254 -j ACCEPT


WORKER_DATA_DISK_NAME="{worker_data_disk_name}"

# format local SSD
sudo mkfs.xfs -m reflink=1 /dev/$WORKER_DATA_DISK_NAME
sudo mkdir -p /mnt/disks/$WORKER_DATA_DISK_NAME
sudo mount -o prjquota /dev/$WORKER_DATA_DISK_NAME /mnt/disks/$WORKER_DATA_DISK_NAME
sudo chmod a+w /mnt/disks/$WORKER_DATA_DISK_NAME
XFS_DEVICE=$(xfs_info /mnt/disks/$WORKER_DATA_DISK_NAME | head -n 1 | awk '{{ print $1 }}' | awk  'BEGIN {{ FS = "=" }}; {{ print $2 }}')

# reconfigure docker to use local SSD
sudo service docker stop
sudo mv /var/lib/docker /mnt/disks/$WORKER_DATA_DISK_NAME/docker
sudo ln -s /mnt/disks/$WORKER_DATA_DISK_NAME/docker /var/lib/docker
sudo service docker start

# reconfigure /batch and /logs and /gcsfuse to use local SSD
sudo mkdir -p /mnt/disks/$WORKER_DATA_DISK_NAME/batch/
sudo ln -s /mnt/disks/$WORKER_DATA_DISK_NAME/batch /batch

sudo mkdir -p /mnt/disks/$WORKER_DATA_DISK_NAME/logs/
sudo ln -s /mnt/disks/$WORKER_DATA_DISK_NAME/logs /logs

sudo mkdir -p /mnt/disks/$WORKER_DATA_DISK_NAME/gcsfuse/
sudo ln -s /mnt/disks/$WORKER_DATA_DISK_NAME/gcsfuse /gcsfuse

sudo mkdir -p /mnt/disks/$WORKER_DATA_DISK_NAME/xfsquota/
sudo ln -s /mnt/disks/$WORKER_DATA_DISK_NAME/xfsquota /xfsquota

touch /xfsquota/projects
touch /xfsquota/projid

ln -s /xfsquota/projects /etc/projects
ln -s /xfsquota/projid /etc/projid

export HOME=/root

CORES=$(nproc)
NAMESPACE=$(curl -s -H "Metadata-Flavor: Google" "http://metadata.google.internal/computeMetadata/v1/instance/attributes/namespace")
ACTIVATION_TOKEN=$(curl -s -H "Metadata-Flavor: Google" "http://metadata.google.internal/computeMetadata/v1/instance/attributes/activation_token")
IP_ADDRESS=$(curl -s -H "Metadata-Flavor: Google" "http://metadata.google.internal/computeMetadata/v1/instance/network-interfaces/0/ip")
PROJECT=$(curl -s -H "Metadata-Flavor: Google" "http://metadata.google.internal/computeMetadata/v1/project/project-id")

BATCH_LOGS_BUCKET_NAME=$(curl -s -H "Metadata-Flavor: Google" "http://metadata.google.internal/computeMetadata/v1/instance/attributes/batch_logs_bucket_name")
INSTANCE_ID=$(curl -s -H "Metadata-Flavor: Google" "http://metadata.google.internal/computeMetadata/v1/instance/attributes/instance_id")
WORKER_CONFIG=$(curl -s -H "Metadata-Flavor: Google" "http://metadata.google.internal/computeMetadata/v1/instance/attributes/worker_config")
MAX_IDLE_TIME_MSECS=$(curl -s -H "Metadata-Flavor: Google" "http://metadata.google.internal/computeMetadata/v1/instance/attributes/max_idle_time_msecs")
NAME=$(curl -s http://metadata.google.internal/computeMetadata/v1/instance/name -H 'Metadata-Flavor: Google')
ZONE=$(curl -s http://metadata.google.internal/computeMetadata/v1/instance/zone -H 'Metadata-Flavor: Google')

BATCH_WORKER_IMAGE=$(curl -s -H "Metadata-Flavor: Google" "http://metadata.google.internal/computeMetadata/v1/instance/attributes/batch_worker_image")

# Setup fluentd
touch /worker.log
touch /run.log

sudo rm /etc/google-fluentd/config.d/*  # remove unused config files

sudo tee /etc/google-fluentd/config.d/syslog.conf <<EOF
<source>
@type tail
format syslog
path /var/log/syslog
pos_file /var/lib/google-fluentd/pos/syslog.pos
read_from_head true
tag syslog
</source>
EOF

sudo tee /etc/google-fluentd/config.d/worker-log.conf <<EOF {{
<source>
@type tail
format json
path /worker.log
pos_file /var/lib/google-fluentd/pos/worker-log.pos
read_from_head true
tag worker.log
</source>

<filter worker.log>
@type record_transformer
enable_ruby
<record>
    severity \${{ record["levelname"] }}
    timestamp \${{ record["asctime"] }}
</record>
</filter>
EOF

sudo tee /etc/google-fluentd/config.d/run-log.conf <<EOF
<source>
@type tail
format none
path /run.log
pos_file /var/lib/google-fluentd/pos/run-log.pos
read_from_head true
tag run.log
</source>
EOF

sudo cp /etc/google-fluentd/google-fluentd.conf /etc/google-fluentd/google-fluentd.conf.bak
head -n -1 /etc/google-fluentd/google-fluentd.conf.bak | sudo tee /etc/google-fluentd/google-fluentd.conf
sudo tee -a /etc/google-fluentd/google-fluentd.conf <<EOF
labels {{
"namespace": "$NAMESPACE",
"instance_id": "$INSTANCE_ID"
}}
</match>
EOF
rm /etc/google-fluentd/google-fluentd.conf.bak

sudo service google-fluentd restart

# retry once
docker pull $BATCH_WORKER_IMAGE || \
(echo 'pull failed, retrying' && sleep 15 && docker pull $BATCH_WORKER_IMAGE)

# So here I go it's my shot.
docker run \
-e CORES=$CORES \
-e NAME=$NAME \
-e NAMESPACE=$NAMESPACE \
-e ACTIVATION_TOKEN=$ACTIVATION_TOKEN \
-e IP_ADDRESS=$IP_ADDRESS \
-e BATCH_LOGS_BUCKET_NAME=$BATCH_LOGS_BUCKET_NAME \
-e INSTANCE_ID=$INSTANCE_ID \
-e PROJECT=$PROJECT \
-e WORKER_CONFIG=$WORKER_CONFIG \
-e MAX_IDLE_TIME_MSECS=$MAX_IDLE_TIME_MSECS \
-e WORKER_DATA_DISK_MOUNT=/mnt/disks/$WORKER_DATA_DISK_NAME \
-v /var/run/docker.sock:/var/run/docker.sock \
-v /usr/bin/docker:/usr/bin/docker \
-v /usr/sbin/xfs_quota:/usr/sbin/xfs_quota \
-v /batch:/batch \
-v /logs:/logs \
-v /gcsfuse:/gcsfuse:shared \
-v /xfsquota:/xfsquota \
--mount type=bind,source=/mnt/disks/$WORKER_DATA_DISK_NAME,target=/host \
-p 5000:5000 \
--device /dev/fuse \
--device $XFS_DEVICE \
--cap-add SYS_ADMIN \
--security-opt apparmor:unconfined \
--network batch-worker \
$BATCH_WORKER_IMAGE \
python3 -u -m batch.worker.worker >worker.log 2>&1

[ $? -eq 0 ] || tail -n 1000 worker.log

while true; do
gcloud -q compute instances delete $NAME --zone=$ZONE
sleep 1
done
'''
            }, {
                'key': 'shutdown-script',
                'value': '''
set -x

INSTANCE_ID=$(curl -s -H "Metadata-Flavor: Google" "http://metadata.google.internal/computeMetadata/v1/instance/attributes/instance_id")
NAME=$(curl -s http://metadata.google.internal/computeMetadata/v1/instance/name -H 'Metadata-Flavor: Google')

journalctl -u docker.service > dockerd.log
'''
            }, {
                'key': 'activation_token',
                'value': activation_token
            }, {
                'key': 'batch_worker_image',
                'value': BATCH_WORKER_IMAGE
            }, {
                'key': 'namespace',
                'value': DEFAULT_NAMESPACE
            }, {
                'key': 'batch_logs_bucket_name',
                'value': log_store.batch_logs_bucket_name
            }, {
                'key': 'instance_id',
                'value': log_store.instance_id
            }, {
                'key': 'max_idle_time_msecs',
                'value': max_idle_time_msecs
            }]
        },
        'tags': {
            'items': [
                "batch2-agent"
            ]
        },
    }

    worker_config = WorkerConfig.from_instance_config(config)
    assert worker_config.is_valid_configuration(app['resources'])
    config['metadata']['items'].append({
        'key': 'worker_config',
        'value': base64.b64encode(json.dumps(worker_config.config).encode()).decode()
    })

    await compute_client.post(
        f'/zones/{zone}/instances', json=config)

    log.info(f'created machine {machine_name}')

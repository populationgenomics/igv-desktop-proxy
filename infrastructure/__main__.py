import pulumi
import pulumi_docker as docker
import pulumi_gcp as gcp
from pulumi import Config, get_stack
from pulumi_docker import BuilderVersion

stack = get_stack()
gcp_config = Config('gcp')
app_config = Config('app')

# set up artifact registry
gcp.artifactregistry.Repository(
    'igv-desktop-proxy-repository',
    location=gcp_config.require('region'),
    repository_id=f'igv-desktop-proxy-repository-{stack}',
    format='DOCKER',
    description='igv-desktop-proxy docker repository',
    # Remove any versions that are older than 30 days and are untagged
    # this means that the latest version will always be kept but older
    # versions that are already deployed will not.
    cleanup_policies=[
        {
            'id': 'delete-untagged',
            'action': 'DELETE',
            'condition': {'tag_state': 'UNTAGGED', 'older_than': '30d'},
        },
    ],
)


image = docker.Image(
    'igv-desktop-proxy-image',
    image_name=f'{gcp_config.require("region")}-docker.pkg.dev/{gcp_config.require("project")}/igv-desktop-proxy-repository-{stack}/igv-desktop-proxy-image:latest',
    build=docker.DockerBuildArgs(
        context='../',
        dockerfile='Dockerfile',
        args={
            'BUILDKIT_INLINE_CACHE': '1',
        },
        builder_version=BuilderVersion.BUILDER_BUILD_KIT,
        platform='linux/amd64',
    ),
)


service_account = gcp.serviceaccount.Account(
    'igv-desktop-proxy-service-account',
    account_id=f'igv-desktop-proxy-{stack}',
    display_name=f'IGV Desktop Proxy ({stack})',
)

# creating vpc for redis connection
network = gcp.compute.Network(
    'igv-desktop-proxy-network',
    name=f'igv-proxy-network-{stack}',
    auto_create_subnetworks=False,
)

subnetwork = gcp.compute.Subnetwork(
    'igv-desktop-proxy-subnetwork',
    name=f'igv-proxy-subnetwork-{stack}',
    ip_cidr_range='10.0.0.0/24',
    region=gcp_config.require('region'),
    network=network.id,
)

redis_instance = gcp.redis.Instance(
    'igv-desktop-proxy-redis',
    name=f'igv-proxy-redis-{stack}',
    memory_size_gb=1,
    tier='BASIC',
    redis_version='REDIS_7_2',
    region=gcp_config.require('region'),
    authorized_network=network.id,
    auth_enabled=True,
)

# provide secret manager access to the service manager
gcp.secretmanager.SecretIamMember(
    'igv-desktop-proxy-redis-secret-accessor',
    project=gcp_config.require('project'),
    secret_id='redis-password',
    role='roles/secretmanager.secretAccessor',
    member=service_account.email.apply(lambda e: f'serviceAccount:{e}'),
)

cloud_run = gcp.cloudrunv2.Service(
    'igv-desktop-proxy',
    name=f'igv-desktop-proxy-{stack}',
    ingress='INGRESS_TRAFFIC_INTERNAL_LOAD_BALANCER',
    location=gcp_config.require('region'),
    default_uri_disabled=True,
    template=gcp.cloudrunv2.ServiceTemplateArgs(
        service_account=service_account.email,
        timeout='3600s',  # Max cloudrun timeout is 1 hour
        scaling=gcp.cloudrunv2.ServiceTemplateScalingArgs(
            min_instance_count=0,
            max_instance_count=10,
        ),
        vpc_access=gcp.cloudrunv2.ServiceTemplateVpcAccessArgs(
            network_interfaces=[
                gcp.cloudrunv2.ServiceTemplateVpcAccessNetworkInterfaceArgs(
                    network=network.id,
                    subnetwork=subnetwork.id,
                ),
            ],
            egress='PRIVATE_RANGES_ONLY',
        ),
        containers=[
            gcp.cloudrunv2.ServiceTemplateContainerArgs(
                image=image.repo_digest,
                resources=gcp.cloudrunv2.ServiceTemplateContainerResourcesArgs(
                    limits={
                        'memory': '4Gi',
                        'cpu': '2',
                    },
                    startup_cpu_boost=True,  # Allocate extra CPU during startup to improve cold start times
                ),
                envs=[
                    gcp.cloudrunv2.ServiceTemplateContainerEnvArgs(
                        name='REDIS_HOST',
                        value=redis_instance.host,
                    ),
                    gcp.cloudrunv2.ServiceTemplateContainerEnvArgs(
                        name='REDIS_PORT',
                        value=redis_instance.port.apply(lambda p: str(p)),
                    ),
                    gcp.cloudrunv2.ServiceTemplateContainerEnvArgs(
                        name='REDIS_PASSWORD',
                        value_source=gcp.cloudrunv2.ServiceTemplateContainerEnvValueSourceArgs(
                            secret_key_ref=gcp.cloudrunv2.ServiceTemplateContainerEnvValueSourceSecretKeyRefArgs(
                                secret='redis-password',
                                version='latest',
                            )
                        ),
                    ),
                ],
                ports=gcp.cloudrunv2.ServiceTemplateContainerPortsArgs(
                    name='http1',
                    container_port=8080,
                ),
            ),
        ],
    ),
)

# Allow cloud run unauthenticated access
gcp.cloudrunv2.ServiceIamMember(
    'igv-desktop-proxy-public-access-binding',
    project=cloud_run.project,
    location=cloud_run.location,
    name=cloud_run.name,
    role='roles/run.invoker',
    member='allUsers',
)


neg = gcp.compute.RegionNetworkEndpointGroup(
    'igv-desktop-proxy-neg',
    network_endpoint_type='SERVERLESS',
    region=gcp_config.require('region'),
    cloud_run=gcp.compute.RegionNetworkEndpointGroupCloudRunArgs(
        service=cloud_run.name,
    ),
)


backend_service = gcp.compute.BackendService(
    'igv-desktop-proxy-backend-service',
    enable_cdn=False,
    log_config=gcp.compute.BackendServiceLogConfigArgs(enable=True),
    protocol='HTTPS',
    backends=[gcp.compute.BackendServiceBackendArgs(group=neg.id)],
)


ip_address = gcp.compute.GlobalAddress(
    'igv-desktop-proxy-ip-address',
    name=f'igv-desktop-proxy-ip-address-{stack}',
)

ssl_cert = gcp.compute.ManagedSslCertificate(
    'igv-desktop-proxy-ssl-cert',
    name=f'igv-desktop-proxy-ssl-cert-{stack}',
    managed={
        'domains': [app_config.require('domain')],
    },
)

url_map = gcp.compute.URLMap(
    'igv-desktop-proxy-url-map',
    name=f'igv-desktop-proxy-url-map-{stack}',
    default_service=backend_service.id,
)

https_proxy = gcp.compute.TargetHttpsProxy(
    'igv-desktop-proxy-https-proxy',
    name=f'igv-desktop-proxy-https-proxy-{stack}',
    url_map=url_map.id,
    ssl_certificates=[ssl_cert.id],
)

# Setup Global forwarding rule
gcp.compute.GlobalForwardingRule(
    'igv-desktop-proxy-forwarding-rule',
    name=f'igv-desktop-proxy-forwarding-rule-{stack}',
    target=https_proxy.id,
    port_range='443',
    ip_address=ip_address.address,
)


pulumi.export('load balancer ip', ip_address.address)

import os

import pulumi
import pulumi_docker as docker
import pulumi_gcp as gcp
from pulumi import ResourceOptions, get_stack
from pulumi_docker import BuilderVersion

stack = get_stack()

_gcp_region = os.environ['PULUMI_CONFIG_GCP_REGION']
_gcp_project = os.environ['PULUMI_CONFIG_GCP_PROJECT']
_app_domain = os.environ['PULUMI_CONFIG_APP_DOMAIN']
_private_config_stack = os.environ['PULUMI_CONFIG_PRIVATE_CONFIG_STACK']

gcp_provider = gcp.Provider('gcp', project=_gcp_project, region=_gcp_region)
gcp_opts = ResourceOptions(provider=gcp_provider)

# set up artifact registry
gcp.artifactregistry.Repository(
    'igv-desktop-proxy-repository',
    location=_gcp_region,
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
    opts=gcp_opts,
)


image = docker.Image(
    'igv-desktop-proxy-image',
    image_name=f'{_gcp_region}-docker.pkg.dev/{_gcp_project}/igv-desktop-proxy-repository-{stack}/igv-desktop-proxy-image:latest',
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
    opts=gcp_opts,
)

cloud_run = gcp.cloudrunv2.Service(
    'igv-desktop-proxy',
    name=f'igv-desktop-proxy-{stack}',
    ingress='INGRESS_TRAFFIC_INTERNAL_LOAD_BALANCER',
    location=_gcp_region,
    default_uri_disabled=True,
    template=gcp.cloudrunv2.ServiceTemplateArgs(
        service_account=service_account.email,
        timeout='3600s',  # Max cloudrun timeout is 1 hour
        scaling=gcp.cloudrunv2.ServiceTemplateScalingArgs(
            min_instance_count=0,
            max_instance_count=10,
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
                envs=[],
                ports=gcp.cloudrunv2.ServiceTemplateContainerPortsArgs(
                    name='http1',
                    container_port=8080,
                ),
            ),
        ],
    ),
    opts=gcp_opts,
)

# Allow cloud run unauthenticated access
gcp.cloudrunv2.ServiceIamMember(
    'igv-desktop-proxy-public-access-binding',
    project=cloud_run.project,
    location=cloud_run.location,
    name=cloud_run.name,
    role='roles/run.invoker',
    member='allUsers',
    opts=gcp_opts,
)


neg = gcp.compute.RegionNetworkEndpointGroup(
    'igv-desktop-proxy-neg',
    network_endpoint_type='SERVERLESS',
    region=_gcp_region,
    cloud_run=gcp.compute.RegionNetworkEndpointGroupCloudRunArgs(
        service=cloud_run.name,
    ),
    opts=gcp_opts,
)

private_stack = pulumi.StackReference(_private_config_stack)
security_policy_id = private_stack.get_output('security_policy_id')

backend_service = gcp.compute.BackendService(
    'igv-desktop-proxy-backend-service',
    enable_cdn=False,
    log_config=gcp.compute.BackendServiceLogConfigArgs(enable=True),
    protocol='HTTPS',
    security_policy=security_policy_id,
    backends=[gcp.compute.BackendServiceBackendArgs(group=neg.id)],
    opts=gcp_opts,
)


ip_address = gcp.compute.GlobalAddress(
    'igv-desktop-proxy-ip-address',
    name=f'igv-desktop-proxy-ip-address-{stack}',
    opts=gcp_opts,
)

ssl_cert = gcp.compute.ManagedSslCertificate(
    'igv-desktop-proxy-ssl-cert',
    name=f'igv-desktop-proxy-ssl-cert-{stack}',
    managed={
        'domains': [_app_domain],
    },
    opts=gcp_opts,
)

url_map = gcp.compute.URLMap(
    'igv-desktop-proxy-url-map',
    name=f'igv-desktop-proxy-url-map-{stack}',
    default_service=backend_service.id,
    opts=gcp_opts,
)

https_proxy = gcp.compute.TargetHttpsProxy(
    'igv-desktop-proxy-https-proxy',
    name=f'igv-desktop-proxy-https-proxy-{stack}',
    url_map=url_map.id,
    ssl_certificates=[ssl_cert.id],
    opts=gcp_opts,
)

# Setup Global forwarding rule
gcp.compute.GlobalForwardingRule(
    'igv-desktop-proxy-forwarding-rule',
    name=f'igv-desktop-proxy-forwarding-rule-{stack}',
    target=https_proxy.id,
    port_range='443',
    ip_address=ip_address.address,
    opts=gcp_opts,
)


pulumi.export('load balancer ip', ip_address.address)

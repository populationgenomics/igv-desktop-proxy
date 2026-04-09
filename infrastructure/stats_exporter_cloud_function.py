import os

import pulumi
import pulumi_gcp as gcp
from pulumi import ResourceOptions


def create_download_stats_exporter_resources(  # noqa: PLR0913
    stack: str,
    redis_instance: gcp.redis.Instance,
    redis_password_secret: gcp.secretmanager.Secret,
    redis_password_version: gcp.secretmanager.SecretVersion,
    network: gcp.compute.Network,
    subnetwork: gcp.compute.Subnetwork,
    gcp_provider: gcp.Provider,
) -> None:
    """Create Cloud Function and Cloud Scheduler resources to export download stats."""
    _stats_archive_bucket = os.environ['APP_CONFIG_STATS_ARCHIVE_BUCKET']
    _cloud_function_source_bucket = os.environ['APP_CONFIG_CLOUD_FUNCTION_SOURCE_BUCKET']

    gcp_opts = ResourceOptions(provider=gcp_provider)
    _gcp_region = gcp_provider.region
    _gcp_project = gcp_provider.project

    # create the bucket to store download stats via proxy
    download_stats_archive_bucket = gcp.storage.Bucket(
        'download-stats-archive-bucket',
        name=_stats_archive_bucket,
        location=_gcp_region,
        uniform_bucket_level_access=True,
        lifecycle_rules=[
            gcp.storage.BucketLifecycleRuleArgs(
                action=gcp.storage.BucketLifecycleRuleActionArgs(type='Delete'),
                condition=gcp.storage.BucketLifecycleRuleConditionArgs(
                    age=90,
                ),  # retain objects up tp 90 days for auditing
            ),
        ],
        opts=gcp_opts,
    )

    # create service account
    download_stats_exporter_sa = gcp.serviceaccount.Account(
        'download-stats-export-service-account',
        account_id=f'download-stats-exporter-{stack}',
        display_name=f'IGV desktop proxy download stats exporter ({stack})',
        opts=gcp_opts,
    )

    # Allow the stats exporter SA to write objects to the stats GCS bucket
    gcp.storage.BucketIAMMember(
        'download-stats-exporter-gcs-writer',
        bucket=download_stats_archive_bucket.name,
        role='roles/storage.objectCreator',
        member=download_stats_exporter_sa.email.apply(lambda e: f'serviceAccount:{e}'),
        opts=gcp_opts,
    )

    # Allow the service account to read the Redis password secret
    redis_iam = gcp.secretmanager.SecretIamMember(
        'download-stats-exporter-redis-secret-accessor',
        project=_gcp_project,
        secret_id=redis_password_secret.secret_id,
        role='roles/secretmanager.secretAccessor',
        member=download_stats_exporter_sa.email.apply(lambda e: f'serviceAccount:{e}'),
        opts=gcp_opts,
    )

    # GCS bucket to hold the Cloud Function source archive
    source_bucket = gcp.storage.Bucket(
        'stats-exporter-cloud-function-source-bucket',
        name=_cloud_function_source_bucket,
        location=_gcp_region,
        uniform_bucket_level_access=True,
        opts=gcp_opts,
    )

    # Upload the cloud_function source files to its respective bucket
    cloud_function_source = gcp.storage.BucketObject(
        'stats-exporter-cloud-function-source',
        bucket=source_bucket.name,
        name='source.zip',
        source=pulumi.FileAsset('../stats_exporter/source.zip'),
        opts=gcp_opts,
    )

    # Build the environment variables dict,
    env_vars = pulumi.Output.all(
        host=redis_instance.host,
        port=redis_instance.port.apply(lambda p: str(p)),
    ).apply(
        lambda args: {
            'REDIS_HOST': args['host'],
            'REDIS_PORT': args['port'],
            'GCS_STATS_BUCKET': _stats_archive_bucket,
        },
    )

    # Cloud Function Gen 2
    cloud_function = gcp.cloudfunctionsv2.Function(
        'download-stats-exporter-function',
        name=f'igv-desktop-proxy-download-stats-exporter-{stack}',
        location=_gcp_region,
        build_config=gcp.cloudfunctionsv2.FunctionBuildConfigArgs(
            runtime='python311',
            entry_point='export_download_stats',
            source=gcp.cloudfunctionsv2.FunctionBuildConfigSourceArgs(
                storage_source=gcp.cloudfunctionsv2.FunctionBuildConfigSourceStorageSourceArgs(
                    bucket=source_bucket.name,
                    object=cloud_function_source.name,
                ),
            ),
        ),
        service_config=gcp.cloudfunctionsv2.FunctionServiceConfigArgs(
            available_memory='512M',
            service_account_email=download_stats_exporter_sa.email,
            ingress_settings='ALLOW_INTERNAL_AND_GCLB',
            direct_vpc_egress='VPC_EGRESS_PRIVATE_RANGES_ONLY',
            direct_vpc_network_interfaces=[
                gcp.cloudfunctionsv2.FunctionServiceConfigDirectVpcNetworkInterfaceArgs(
                    network=network.id,
                    subnetwork=subnetwork.id,
                ),
            ],
            environment_variables=env_vars,
            secret_environment_variables=[
                gcp.cloudfunctionsv2.FunctionServiceConfigSecretEnvironmentVariableArgs(
                    key='REDIS_PASSWORD',
                    project_id=_gcp_project,
                    secret=redis_password_secret.secret_id,
                    version='latest',
                ),
            ],
        ),
        opts=ResourceOptions(
            provider=gcp_provider,
            depends_on=[redis_iam, redis_password_version],
        ),
    )

    # service account for the cloud scheduler that export data from redis to GCS
    scheduler_sa = gcp.serviceaccount.Account(
        'download-stats-export-scheduler-sa',
        account_id=f'dl-stats-export-scheduler-{stack}',
        display_name=f'IGV desktop proxy download stats export scheduler ({stack})',
        opts=gcp_opts,
    )

    # Allow Cloud Scheduler to invoke the Cloud Function
    gcp.cloudfunctionsv2.FunctionIamMember(
        'stats-export-scheduler-invoker',
        project=_gcp_project,
        location=_gcp_region,
        cloud_function=cloud_function.name,
        role='roles/cloudfunctions.invoker',
        member=scheduler_sa.email.apply(lambda e: f'serviceAccount:{e}'),
        opts=gcp_opts,
    )

    # Cloud Scheduler job — triggers the function daily at 20:00 UTC (06:00 AEST)
    gcp.cloudscheduler.Job(
        'download-stats-export-scheduler',
        name=f'igv-desktop-proxy-dowload-stats-export-scheduler-{stack}',
        region=_gcp_region,
        opts=gcp_opts,
        description='Triggers daily download stats export from Redis to GCS',
        schedule='0 20 * * *',
        time_zone='UTC',
        attempt_deadline='600s',  # wait up to for the cloud function to finish
        retry_config=gcp.cloudscheduler.JobRetryConfigArgs(
            retry_count=3,
            min_backoff_duration='60s',  # wait at least 60 seconds before firing the first
            max_backoff_duration='3600s',  # maximum time it will wait between retries
            max_retry_duration='0s',
        ),
        http_target=gcp.cloudscheduler.JobHttpTargetArgs(
            # Invoke the cloud function by sending an HTTP POST request to the URL
            uri=cloud_function.url,
            http_method='POST',
            oidc_token=gcp.cloudscheduler.JobHttpTargetOidcTokenArgs(
                service_account_email=scheduler_sa.email,
                audience=cloud_function.url,
            ),
        ),
    )

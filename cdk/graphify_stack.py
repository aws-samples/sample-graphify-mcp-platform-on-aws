"""Graphify MCP platform stack (ECS Fargate query plane).

Build plane : EventBridge rate(5 min) -> poller Lambda -> CodeBuild (ARM64,
              NO_SOURCE) -> S3 graphs bucket; completion Lambda drives
              DynamoDB state off the CodeBuild state-change event.
Query plane : ECS Fargate services (linux/arm64) running graphify's own MCP
              server behind a thin S3-sync entrypoint — one always-warm task
              per repo plus a hub task for the merged public graph. The graph
              loads ONCE at task start and stays resident (the earlier fixed-memory
              microVM runtime OOM-crash-looped on large graphs and re-paid the
              cold load per call); graph updates still hot-reload via the S3
              ETag poll, no redeploy. Tasks sit in public subnets (no NAT;
              outbound-only for ECR pulls, S3 rides the gateway endpoint) and
              their security group admits ONLY the data-plane proxy Lambda —
              nothing is world-accessible.
"""

import re

from aws_cdk import (
    CfnOutput,
    Duration,
    RemovalPolicy,
    Stack,
    aws_apigateway as apigw,
    aws_apigatewayv2 as apigwv2,
    aws_apigatewayv2_authorizers as apigwv2_authorizers,
    aws_apigatewayv2_integrations as apigwv2_integrations,
    aws_cloudfront as cloudfront,
    aws_cloudfront_origins as origins,
    aws_codebuild as codebuild,
    aws_cognito as cognito,
    aws_dynamodb as dynamodb,
    aws_ec2 as ec2,
    aws_ecr_assets as ecr_assets,
    aws_ecs as ecs,
    aws_events as events,
    aws_events_targets as targets,
    aws_iam as iam,
    aws_lambda as lambda_,
    aws_logs as logs,
    aws_s3 as s3,
    aws_s3_deployment as s3_deployment,
    aws_secretsmanager as secretsmanager,
    aws_servicediscovery as servicediscovery,
)
from constructs import Construct

from buildspec import BUILD_SPEC


class GraphifyMcpPlatformStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        runtime_name = self.node.try_get_context("runtime_name") or "graphify_mcp"
        # Naming stem for the CodeBuild project / data API; the charset is
        # restricted so derived resource names never churn.
        if not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_]{0,47}", runtime_name):
            raise ValueError(f"invalid runtime_name {runtime_name!r} (pattern: [a-zA-Z][a-zA-Z0-9_]{{0,47}})")
        default_repo_id = self.node.try_get_context("default_repo_id") or ""
        github_token_secret_arn = self.node.try_get_context("github_token_secret_arn") or ""

        # Graph-build container size, chosen at deploy time via
        #   -c build_compute=small|medium|large
        # (ARM tiers: small=2vCPU/4GB, medium=4/8, large=8/16). graphify's
        # in-memory graph assembly OOMs the 4GB SMALL tier on large repos
        # (e.g. LiteLLM was SIGKILLed mid-extract), so the default is LARGE
        # (16GB). Dial down to small for a personal, small-repo deployment to
        # stay in the CodeBuild free tier.
        _COMPUTE_TIERS = {
            "small": codebuild.ComputeType.SMALL,
            "medium": codebuild.ComputeType.MEDIUM,
            "large": codebuild.ComputeType.LARGE,
        }
        build_compute = (self.node.try_get_context("build_compute") or "large").lower()
        if build_compute not in _COMPUTE_TIERS:
            raise ValueError(
                f"invalid build_compute {build_compute!r} (choose one of {sorted(_COMPUTE_TIERS)})"
            )
        build_compute_type = _COMPUTE_TIERS[build_compute]

        # ------------------------------------------------------------------
        # Storage
        # ------------------------------------------------------------------
        bucket = s3.Bucket(
            self,
            "GraphsBucket",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            versioned=True,
            encryption=s3.BucketEncryption.S3_MANAGED,
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
            lifecycle_rules=[
                s3.LifecycleRule(
                    id="expire-noncurrent",
                    prefix="repos/",
                    noncurrent_version_expiration=Duration.days(7),
                    abort_incomplete_multipart_upload_after=Duration.days(1),
                ),
                s3.LifecycleRule(
                    # files-source upload prefixes: every re-sync of a changed
                    # file leaves a noncurrent version behind on this versioned
                    # bucket — reap them (and purge/delete markers) or repeated
                    # syncs accumulate storage forever.
                    id="expire-uploads-noncurrent",
                    prefix="uploads/",
                    noncurrent_version_expiration=Duration.days(7),
                    expired_object_delete_marker=True,
                    abort_incomplete_multipart_upload_after=Duration.days(1),
                ),
                s3.LifecycleRule(
                    # history/<repo_id>/<sha>/graph.json copies (graph_diff /
                    # rollback). The bucket is versioned, so both current and
                    # noncurrent versions need expiry.
                    id="expire-sha-history",
                    prefix="history/",
                    expiration=Duration.days(30),
                    noncurrent_version_expiration=Duration.days(1),
                ),
                s3.LifecycleRule(
                    # S3 requires ExpiredObjectDeleteMarker in its own rule.
                    id="reap-history-delete-markers",
                    prefix="history/",
                    expired_object_delete_marker=True,
                ),
            ],
        )

        table = dynamodb.Table(
            self,
            "RepoRegistry",
            partition_key=dynamodb.Attribute(name="repo_id", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            point_in_time_recovery_specification=dynamodb.PointInTimeRecoverySpecification(
                point_in_time_recovery_enabled=True
            ),
            removal_policy=RemovalPolicy.DESTROY,
        )
        table.add_global_secondary_index(
            index_name="due-index",
            partition_key=dynamodb.Attribute(name="enabled", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="next_poll_at", type=dynamodb.AttributeType.NUMBER),
        )

        # ------------------------------------------------------------------
        # Build plane: one CodeBuild project for every repo (NO_SOURCE)
        # ------------------------------------------------------------------
        build_project = codebuild.Project(
            self,
            "GraphBuild",
            project_name=f"{runtime_name}_graph_build",
            build_spec=codebuild.BuildSpec.from_object_to_yaml(BUILD_SPEC),
            environment=codebuild.BuildEnvironment(
                build_image=codebuild.LinuxArmBuildImage.AMAZON_LINUX_2023_STANDARD_3_0,
                compute_type=build_compute_type,
            ),
            # LLM_EXTRACT builds on large doc corpora (dozens of PDFs with
            # recursive truncation splits) can exceed 30 min; llmcache only
            # skips chunks that completed, so a short timeout never converges.
            timeout=Duration.minutes(60),
            description="Clones a registered git repo at a pinned SHA and publishes its graphify graph to S3",
            environment_variables={
                "REGISTRY_TABLE": codebuild.BuildEnvironmentVariable(value=table.table_name),
            },
        )
        # Read to restore the incremental baseline, put to publish; the build
        # never deletes objects, so no s3:DeleteObject*.
        bucket.grant_read(build_project, "repos/*")
        bucket.grant_put(build_project, "repos/*")
        bucket.grant_put(build_project, "history/*")
        # The explorer-bundle step removes a stale viz bundle when make_viz.py
        # fails (buildspec `|| { …; aws s3 rm … }`). Delete stays scoped to the
        # two bundle objects — graph.json / snapshots remain non-deletable.
        bucket.grant_delete(build_project, "repos/*/latest/graphify-out/viz.json")
        bucket.grant_delete(build_project, "repos/*/latest/graphify-out/viz-meta.json")
        # files-source builds download the user-synced corpus (fetch_uploads.py).
        bucket.grant_read(build_project, "uploads/*")
        # Non-git build helpers (crawler / uploads fetch / extract driver) ride
        # in the bucket, NOT inline in the buildspec: the project is NO_SOURCE
        # and CodeBuild caps an inline buildspec at 25,600 chars.
        bucket.grant_read(build_project, "assets/*")
        s3_deployment.BucketDeployment(
            self,
            "BuildScriptsDeployment",
            destination_bucket=bucket,
            destination_key_prefix="assets/build_scripts",
            sources=[s3_deployment.Source.asset("cdk/build_scripts", exclude=["__pycache__", "*.pyc"])],
            prune=True,
        )
        # Read-only registry access: the merged-__all__ step queries enabled
        # repos; state transitions remain the completion Lambda's job.
        table.grant_read_data(build_project)
        # Per-repo clone credentials under the graphify/ secret prefix.
        build_project.add_to_role_policy(
            iam.PolicyStatement(
                actions=["secretsmanager:GetSecretValue"],
                resources=[f"arn:aws:secretsmanager:{self.region}:{self.account}:secret:graphify/*"],
            )
        )
        # Document-source LLM extraction (LLM_EXTRACT=1): graphify's bedrock
        # backend calls Converse on the global Sonnet 5 inference profile.
        # Cross-region (global.) profiles resolve to foundation models in
        # other regions, so both ARN families are needed (same shape as the
        # playground's bedrock_invoke).
        build_project.add_to_role_policy(
            iam.PolicyStatement(
                actions=["bedrock:InvokeModel"],
                resources=[
                    "arn:aws:bedrock:*::foundation-model/anthropic.*",
                    f"arn:aws:bedrock:*:{self.account}:inference-profile/*.anthropic.*",
                ],
            )
        )

        # ------------------------------------------------------------------
        # Poller
        # ------------------------------------------------------------------
        poller = lambda_.Function(
            self,
            "Poller",
            runtime=lambda_.Runtime.PYTHON_3_12,
            architecture=lambda_.Architecture.ARM_64,
            handler="handler.handler",
            code=lambda_.Code.from_asset("lambdas/poller"),
            timeout=Duration.seconds(120),
            memory_size=256,
            environment={
                "TABLE_NAME": table.table_name,
                "PROJECT_NAME": build_project.project_name,
                "GRAPH_BUCKET": bucket.bucket_name,
                "GITHUB_TOKEN_SECRET_ARN": github_token_secret_arn,
            },
        )
        table.grant_read_write_data(poller)
        # files-source change detection lists uploads/<repo_id>/ (ETag manifest).
        bucket.grant_read(poller, "uploads/*")
        poller.add_to_role_policy(
            iam.PolicyStatement(
                # BatchGetBuilds backs the stale-claim liveness check.
                actions=["codebuild:StartBuild", "codebuild:BatchGetBuilds"],
                resources=[build_project.project_arn],
            )
        )
        poller.add_to_role_policy(
            iam.PolicyStatement(
                actions=["secretsmanager:GetSecretValue"],
                resources=[f"arn:aws:secretsmanager:{self.region}:{self.account}:secret:graphify/*"]
                + ([github_token_secret_arn] if github_token_secret_arn else []),
            )
        )
        events.Rule(
            self,
            "PollTick",
            schedule=events.Schedule.rate(Duration.minutes(5)),
            targets=[targets.LambdaFunction(poller)],
        )

        # ------------------------------------------------------------------
        # Completion
        # ------------------------------------------------------------------
        completion = lambda_.Function(
            self,
            "Completion",
            runtime=lambda_.Runtime.PYTHON_3_12,
            architecture=lambda_.Architecture.ARM_64,
            handler="handler.handler",
            code=lambda_.Code.from_asset("lambdas/completion"),
            timeout=Duration.seconds(60),
            memory_size=256,
            environment={
                "TABLE_NAME": table.table_name,
                "GRAPH_BUCKET": bucket.bucket_name,
            },
        )
        table.grant_read_write_data(completion)
        bucket.grant_read(completion, "repos/*")
        events.Rule(
            self,
            "BuildStateChange",
            event_pattern=events.EventPattern(
                source=["aws.codebuild"],
                detail_type=["CodeBuild Build State Change"],
                detail={
                    "project-name": [build_project.project_name],
                    "build-status": ["SUCCEEDED", "FAILED", "FAULT", "TIMED_OUT", "STOPPED"],
                },
            ),
            targets=[targets.LambdaFunction(completion)],
        )

        # ------------------------------------------------------------------
        # Webhook path: push-triggered builds for OWNED repos
        # (trigger=webhook registrations; polling stays the default)
        # ------------------------------------------------------------------
        webhook_secret = secretsmanager.Secret(
            self,
            "WebhookHmacSecret",
            description="Shared HMAC secret for the GitHub push webhook endpoint (X-Hub-Signature-256)",
            generate_secret_string=secretsmanager.SecretStringGenerator(
                exclude_punctuation=True, password_length=48
            ),
            removal_policy=RemovalPolicy.DESTROY,
        )
        webhook = lambda_.Function(
            self,
            "Webhook",
            runtime=lambda_.Runtime.PYTHON_3_12,
            architecture=lambda_.Architecture.ARM_64,
            handler="handler.handler",
            code=lambda_.Code.from_asset("lambdas/webhook"),
            timeout=Duration.seconds(30),
            memory_size=256,
            environment={
                "TABLE_NAME": table.table_name,
                "PROJECT_NAME": build_project.project_name,
                "GRAPH_BUCKET": bucket.bucket_name,
                "WEBHOOK_SECRET_ARN": webhook_secret.secret_arn,
            },
        )
        table.grant_read_write_data(webhook)
        webhook_secret.grant_read(webhook)
        webhook.add_to_role_policy(
            iam.PolicyStatement(actions=["codebuild:StartBuild"], resources=[build_project.project_arn])
        )
        # The public edge is API Gateway, NOT a Lambda Function URL: a NONE-auth
        # Function URL puts Principal "*" on the Lambda resource policy, which
        # security tooling (rightly) flags as a world-accessible Lambda. With
        # API Gateway the Lambda policy is scoped to apigateway.amazonaws.com +
        # this API's SourceArn, and the GitHub HMAC (validated before any
        # parsing) remains the application-level auth gate — the standard way
        # GitHub webhooks are consumed.
        webhook_api = apigwv2.HttpApi(
            self,
            "WebhookApi",
            description="GitHub push-webhook receiver (HMAC-gated) for graphify graph builds",
        )
        webhook_api.add_routes(
            path="/",
            methods=[apigwv2.HttpMethod.POST],
            integration=apigwv2_integrations.HttpLambdaIntegration("WebhookIntegration", webhook),
        )

        # ------------------------------------------------------------------
        # Query plane: ECS Fargate (always-warm graphify MCP servers)
        # ------------------------------------------------------------------
        # Public subnets, NO NAT: tasks need outbound only (ECR image pull;
        # S3/DynamoDB ride the free gateway endpoints). Inbound is closed —
        # the task security group admits the proxy Lambda alone, so despite
        # the public IPs nothing here is world-reachable.
        vpc = ec2.Vpc(
            self,
            "McpVpc",
            max_azs=2,
            nat_gateways=0,
            subnet_configuration=[
                ec2.SubnetConfiguration(name="public", subnet_type=ec2.SubnetType.PUBLIC, cidr_mask=24)
            ],
        )
        vpc.add_gateway_endpoint("S3Endpoint", service=ec2.GatewayVpcEndpointAwsService.S3)
        vpc.add_gateway_endpoint("DynamoEndpoint", service=ec2.GatewayVpcEndpointAwsService.DYNAMODB)

        cluster = ecs.Cluster(self, "McpCluster", vpc=vpc)
        namespace = servicediscovery.PrivateDnsNamespace(
            self, "McpNamespace", name="graphify.internal", vpc=vpc
        )

        proxy_sg = ec2.SecurityGroup(
            self, "McpProxySg", vpc=vpc, description="graphify MCP data-plane proxy Lambda"
        )
        service_sg = ec2.SecurityGroup(
            self, "McpServiceSg", vpc=vpc, description="graphify MCP Fargate tasks"
        )
        service_sg.add_ingress_rule(proxy_sg, ec2.Port.tcp(8000), "MCP data-plane proxy only")

        # linux/arm64 image: graphifyy serving deps + entrypoint.py (S3 sync +
        # code-search wrapper). One image serves the hub and every repo task.
        image = ecr_assets.DockerImageAsset(
            self, "McpImage", directory="runtime", platform=ecr_assets.Platform.LINUX_ARM64
        )

        mcp_log_group = logs.LogGroup(
            self,
            "McpServiceLogs",
            log_group_name=f"/graphify/{runtime_name}/services",
            retention=logs.RetentionDays.ONE_MONTH,
            removal_policy=RemovalPolicy.DESTROY,
        )

        task_role = iam.Role(
            self,
            "McpTaskRole",
            assumed_by=iam.ServicePrincipal("ecs-tasks.amazonaws.com"),
            description="graphify MCP task role (S3 graph/snapshot sync)",
        )
        # GetObject on repos/* plus bucket-level List* (needed for the repo
        # discovery prefix listing) — grant_read with a pattern emits both.
        bucket.grant_read(task_role, "repos/*")
        task_exec_role = iam.Role(
            self,
            "McpTaskExecRole",
            assumed_by=iam.ServicePrincipal("ecs-tasks.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "service-role/AmazonECSTaskExecutionRolePolicy"
                )
            ],
        )

        # Hub: merged all-repos PUBLIC graph (repos/__all__, maintained by the
        # build plane). Per-repo services are created dynamically at
        # registration (lambdas/platform_api/runtimes.py) reusing this image,
        # these roles, and this cluster/namespace.
        # 2 vCPU: the merged graph reloads in the sync thread after every
        # build (127MB ≈ 26s at 1 vCPU); concurrent hub calls queue behind
        # that CPU-bound load, so halving it keeps them under the data
        # plane's timeout budget.
        hub_cpu = int(self.node.try_get_context("hub_cpu") or 2048)
        hub_memory = int(self.node.try_get_context("hub_memory") or 4096)
        hub_task = ecs.FargateTaskDefinition(
            self,
            "HubTaskDef",
            cpu=hub_cpu,
            memory_limit_mib=hub_memory,
            runtime_platform=ecs.RuntimePlatform(
                cpu_architecture=ecs.CpuArchitecture.ARM64,
                operating_system_family=ecs.OperatingSystemFamily.LINUX,
            ),
            task_role=task_role,
            execution_role=task_exec_role,
        )
        hub_task.add_container(
            "mcp",
            image=ecs.ContainerImage.from_docker_image_asset(image),
            logging=ecs.LogDrivers.aws_logs(stream_prefix="hub", log_group=mcp_log_group),
            port_mappings=[ecs.PortMapping(container_port=8000)],
            environment={
                "GRAPH_BUCKET": bucket.bucket_name,
                "GRAPHIFY_MAX_CONTEXTS": "8",
                "SYNC_INTERVAL_SECONDS": "180",
                "DEFAULT_REPO_ID": default_repo_id or "__all__",
                # Pin the hub to the merged PUBLIC graph only. Without this the
                # hub discovers every repos/* prefix in the bucket — including
                # PRIVATE per-repo graphs — and a client could reach one with a
                # tool-call project_path. (The proxy also strips project_path.)
                "REPO_IDS": default_repo_id or "__all__",
            },
        )
        hub_service = ecs.FargateService(
            self,
            "HubService",
            cluster=cluster,
            task_definition=hub_task,
            desired_count=1,
            assign_public_ip=True,
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PUBLIC),
            security_groups=[service_sg],
            # Single memory-heavy task: stop-then-start on deploys (never two
            # 4GB tasks at once); the brief roll gap mirrors a graph reload.
            min_healthy_percent=0,
            max_healthy_percent=100,
            # A crash-looping task must fail the deployment fast (and roll
            # back) instead of wedging CloudFormation for hours.
            circuit_breaker=ecs.DeploymentCircuitBreaker(rollback=True),
            cloud_map_options=ecs.CloudMapOptions(
                name="hub",
                cloud_map_namespace=namespace,
                dns_record_type=servicediscovery.DnsRecordType.A,
                dns_ttl=Duration.seconds(10),
            ),
        )
        service_dns_suffix = namespace.namespace_name
        hub_host = f"hub.{service_dns_suffix}"
        # Comma-joined for Lambda env (per-repo service creation).
        public_subnet_ids = ",".join(s.subnet_id for s in vpc.public_subnets)

        # ==================================================================
        # PLATFORM: API-key MCP data plane + Cognito management console
        # ==================================================================

        # ------------------------------------------------------------------
        # Platform table: users, grants, API keys, usage counters
        # ------------------------------------------------------------------
        platform_table = dynamodb.Table(
            self,
            "PlatformTable",
            partition_key=dynamodb.Attribute(name="pk", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="sk", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            time_to_live_attribute="ttl",
            point_in_time_recovery_specification=dynamodb.PointInTimeRecoverySpecification(
                point_in_time_recovery_enabled=True
            ),
            removal_policy=RemovalPolicy.DESTROY,
        )
        # Reverse lookups (who subscribes to REPO#x); sparse, keys-only.
        platform_table.add_global_secondary_index(
            index_name="entity-index",
            partition_key=dynamodb.Attribute(name="gsi1pk", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="gsi1sk", type=dynamodb.AttributeType.STRING),
            projection_type=dynamodb.ProjectionType.KEYS_ONLY,
        )

        # ------------------------------------------------------------------
        # Console hosting: S3 + CloudFront (OAC). Created before Cognito so
        # the app client can point its callback at the distribution domain.
        # ------------------------------------------------------------------
        console_bucket = s3.Bucket(
            self,
            "ConsoleBucket",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            enforce_ssl=True,
            encryption=s3.BucketEncryption.S3_MANAGED,
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
        )
        distribution = cloudfront.Distribution(
            self,
            "ConsoleDistribution",
            default_behavior=cloudfront.BehaviorOptions(
                origin=origins.S3BucketOrigin.with_origin_access_control(console_bucket),
                viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                cache_policy=cloudfront.CachePolicy.CACHING_OPTIMIZED,
                response_headers_policy=cloudfront.ResponseHeadersPolicy.SECURITY_HEADERS,
            ),
            default_root_object="index.html",
            # OAC at READ-only has no s3:ListBucket, so a missing key is 403;
            # both 403 and 404 must fall back to the SPA shell.
            error_responses=[
                cloudfront.ErrorResponse(
                    http_status=code,
                    response_http_status=200,
                    response_page_path="/index.html",
                    ttl=Duration.seconds(30),
                )
                for code in (403, 404)
            ],
            http_version=cloudfront.HttpVersion.HTTP2_AND_3,
            minimum_protocol_version=cloudfront.SecurityPolicyProtocol.TLS_V1_2_2021,
            comment="graphify platform console",
        )
        console_url = f"https://{distribution.distribution_domain_name}"

        # Browser uploads for files sources: the console POSTs presigned S3
        # forms (minted by the platform API, gated to the caller's own private
        # silo) straight to the graphs bucket. CORS only lets the browser READ
        # the response status — the bucket stays BLOCK_ALL/presigned-only.
        # GET: the graph explorer fetch()es a source's viz bundle / graph.json
        # straight from S3 via short-lived presigned URLs minted by the
        # platform API (GET /repos/{id}/graph). CORS is not the access
        # boundary (it cannot be prefix-scoped) — the presigned URL plus the
        # platform role's repos/* read grant is.
        bucket.add_cors_rule(
            allowed_methods=[s3.HttpMethods.POST, s3.HttpMethods.GET],
            allowed_origins=[console_url, "http://localhost:8787"],
            allowed_headers=["*"],
            exposed_headers=["ETag"],
            max_age=3600,
        )

        # ------------------------------------------------------------------
        # Cognito: invite-only user pool, managed login (v2 + branding)
        # ------------------------------------------------------------------
        user_pool = cognito.UserPool(
            self,
            "PlatformUsers",
            feature_plan=cognito.FeaturePlan.ESSENTIALS,
            self_sign_up_enabled=False,
            sign_in_aliases=cognito.SignInAliases(email=True),
            auto_verify=cognito.AutoVerifiedAttrs(email=True),
            standard_attributes=cognito.StandardAttributes(
                email=cognito.StandardAttribute(required=True, mutable=True)
            ),
            account_recovery=cognito.AccountRecovery.EMAIL_ONLY,
            removal_policy=RemovalPolicy.DESTROY,
        )
        pool_domain = user_pool.add_domain(
            "PlatformDomain",
            cognito_domain=cognito.CognitoDomainOptions(domain_prefix=f"graphify-{self.account}"),
            managed_login_version=cognito.ManagedLoginVersion.NEWER_MANAGED_LOGIN,
        )
        spa_client = user_pool.add_client(
            "ConsoleClient",
            generate_secret=False,
            # ADMIN_USER_PASSWORD enables headless smoke tests (admin-side
            # auth already requires AWS credentials, so it widens nothing).
            auth_flows=cognito.AuthFlow(user_srp=True, admin_user_password=True),
            o_auth=cognito.OAuthSettings(
                flows=cognito.OAuthFlows(authorization_code_grant=True),
                scopes=[cognito.OAuthScope.OPENID, cognito.OAuthScope.EMAIL, cognito.OAuthScope.PROFILE],
                callback_urls=[f"{console_url}/", "http://localhost:8787/"],
                logout_urls=[f"{console_url}/", "http://localhost:8787/"],
            ),
            prevent_user_existence_errors=True,
            access_token_validity=Duration.minutes(60),
            id_token_validity=Duration.minutes(60),
            # 30 days so "keep me signed in" survives across days — the SPA
            # silently exchanges it for fresh 60-min access/id tokens.
            refresh_token_validity=Duration.days(30),
        )
        # Managed login will NOT render for an app client without a branding
        # style — this resource is required, not cosmetic.
        branding = cognito.CfnManagedLoginBranding(
            self,
            "ConsoleBranding",
            user_pool_id=user_pool.user_pool_id,
            client_id=spa_client.user_pool_client_id,
            use_cognito_provided_values=True,
        )
        branding.node.add_dependency(pool_domain)
        cognito.CfnUserPoolGroup(
            self, "AdminGroup", user_pool_id=user_pool.user_pool_id, group_name="admin", precedence=1
        )
        cognito.CfnUserPoolGroup(
            self, "MemberGroup", user_pool_id=user_pool.user_pool_id, group_name="member", precedence=2
        )

        # ------------------------------------------------------------------
        # MCP data plane: REST API + key authorizer + Fargate HTTP proxy
        # (REST, not HTTP API: usage plans / apiKeySource=AUTHORIZER and WAF
        # attachment are REST-only, and those drive per-key throttling.)
        # ------------------------------------------------------------------
        authorizer_fn = lambda_.Function(
            self,
            "KeyAuthorizerFn",
            runtime=lambda_.Runtime.PYTHON_3_12,
            architecture=lambda_.Architecture.ARM_64,
            handler="handler.handler",
            code=lambda_.Code.from_asset("lambdas/authorizer"),
            timeout=Duration.seconds(10),
            memory_size=256,
            environment={
                "PLATFORM_TABLE": platform_table.table_name,
                "REGISTRY_TABLE": table.table_name,
            },
        )
        platform_table.grant_read_data(authorizer_fn)
        table.grant_read_data(authorizer_fn)

        proxy_fn = lambda_.Function(
            self,
            "McpProxyFn",
            runtime=lambda_.Runtime.PYTHON_3_12,
            architecture=lambda_.Architecture.ARM_64,
            handler="handler.handler",
            code=lambda_.Code.from_asset("lambdas/mcp_proxy"),
            # API Gateway cuts buffered integrations at 29s; the longer Lambda
            # timeout only covers the usage-counter writes after a slow call.
            timeout=Duration.seconds(60),
            memory_size=256,
            # In-VPC so it can reach the Fargate tasks over Cloud Map DNS.
            # Public subnets are fine (no NAT to pay for): the Lambda needs
            # only VPC-internal HTTP + DynamoDB via the gateway endpoint, and
            # CloudWatch logging never rides the customer VPC.
            vpc=vpc,
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PUBLIC),
            allow_public_subnet=True,
            security_groups=[proxy_sg],
            environment={
                "PLATFORM_TABLE": platform_table.table_name,
                "REGISTRY_TABLE": table.table_name,
                "HUB_HOST": hub_host,
                "SERVICE_DNS_SUFFIX": service_dns_suffix,
                "SERVICE_PORT": "8000",
            },
        )
        platform_table.grant_write_data(proxy_fn)
        table.grant_read_data(proxy_fn)

        data_api = apigw.RestApi(
            self,
            "McpDataApi",
            rest_api_name=f"{runtime_name}_mcp_data",
            description="API-key MCP data plane for graphify Fargate MCP services",
            endpoint_configuration=apigw.EndpointConfiguration(types=[apigw.EndpointType.REGIONAL]),
            deploy_options=apigw.StageOptions(
                stage_name="v1",
                throttling_rate_limit=100,
                throttling_burst_limit=200,
            ),
            api_key_source_type=apigw.ApiKeySourceType.AUTHORIZER,
            cloud_watch_role=False,
        )
        key_authorizer = apigw.RequestAuthorizer(
            self,
            "ApiKeyAuthorizer",
            handler=authorizer_fn,
            identity_sources=[apigw.IdentitySource.header("X-Graphify-Key")],
            # TTL 0 = revocation is immediate; a cached ALLOW keyed on the
            # header value is exactly what would keep a revoked key alive.
            results_cache_ttl=Duration.seconds(0),
        )
        mcp_resource = data_api.root.add_resource("mcp").add_resource("{serverId}")
        mcp_resource.add_method(
            "POST",
            apigw.LambdaIntegration(proxy_fn, proxy=True),
            authorizer=key_authorizer,
            api_key_required=True,
        )
        # Spec-legal 405 for GET/DELETE so API Gateway never answers an MCP
        # client's stray GET with its own 403 "Missing Authentication Token".
        mock_405 = apigw.MockIntegration(
            request_templates={"application/json": '{"statusCode": 200}'},
            integration_responses=[
                apigw.IntegrationResponse(
                    status_code="405",
                    response_templates={
                        "application/json": '{"jsonrpc":"2.0","id":null,"error":{"code":-32000,"message":"Method Not Allowed: this MCP endpoint accepts POST only"}}'
                    },
                    response_parameters={"method.response.header.Allow": "'POST'"},
                )
            ],
            passthrough_behavior=apigw.PassthroughBehavior.NEVER,
        )
        for verb in ("GET", "DELETE"):
            mcp_resource.add_method(
                verb,
                mock_405,
                method_responses=[
                    apigw.MethodResponse(
                        status_code="405",
                        response_parameters={"method.response.header.Allow": True},
                    )
                ],
            )
        # Gateway responses tuned for MCP clients. A wrong or missing key makes
        # the MCP SDK start OAuth discovery (GET /.well-known/*, POST /register);
        # API Gateway's default for those undefined paths is 403 "Forbidden",
        # which Claude Code caches as "needs authentication" even after the key
        # is fixed. 404 ends discovery cleanly, and the 401/403 bodies say what
        # to do — this API uses a static X-Graphify-Key header, never OAuth.
        data_api.add_gateway_response(
            "UndefinedRouteIs404",
            type=apigw.ResponseType.MISSING_AUTHENTICATION_TOKEN,
            status_code="404",
            templates={"application/json": '{"message":"Not found","hint":"MCP endpoints are POST /v1/mcp/{serverId} with an X-Graphify-Key header; this API has no OAuth endpoints"}'},
        )
        data_api.add_gateway_response(
            "UnauthorizedHint",
            type=apigw.ResponseType.UNAUTHORIZED,
            status_code="401",
            templates={"application/json": '{"message":"Unauthorized","hint":"send a valid API key in the X-Graphify-Key header (issue one in the console, API keys tab); this server does not use OAuth"}'},
        )
        data_api.add_gateway_response(
            "AccessDeniedHint",
            type=apigw.ResponseType.ACCESS_DENIED,
            status_code="403",
            templates={"application/json": '{"message":"Forbidden","hint":"this API key is valid but not scoped to this server (or is revoked/expired); issue a key scoped to it, or an all-servers key"}'},
        )
        usage_plan = data_api.add_usage_plan(
            "StandardTier",
            name=f"{runtime_name}_standard",
            throttle=apigw.ThrottleSettings(rate_limit=20, burst_limit=40),
            quota=apigw.QuotaSettings(limit=500_000, period=apigw.Period.MONTH),
        )
        usage_plan.add_api_stage(stage=data_api.deployment_stage)
        mcp_base_url = f"https://{data_api.rest_api_id}.execute-api.{self.region}.amazonaws.com/v1"

        # ------------------------------------------------------------------
        # Management plane: HTTP API + Cognito JWT authorizer + platform Lambda
        # ------------------------------------------------------------------
        platform_fn = lambda_.Function(
            self,
            "PlatformApiFn",
            runtime=lambda_.Runtime.PYTHON_3_12,
            architecture=lambda_.Architecture.ARM_64,
            handler="handler.handler",
            code=lambda_.Code.from_asset("lambdas/platform_api"),
            timeout=Duration.seconds(28),  # HTTP API integration ceiling is 30s
            memory_size=512,
            environment={
                "PLATFORM_TABLE": platform_table.table_name,
                "REGISTRY_TABLE": table.table_name,
                "PROJECT_NAME": build_project.project_name,
                "GRAPH_BUCKET": bucket.bucket_name,
                "MCP_BASE_URL": mcp_base_url,
                "USAGE_PLAN_ID": usage_plan.usage_plan_id,
                "USER_POOL_ID": user_pool.user_pool_id,
                "WEBHOOK_URL": webhook_api.api_endpoint + "/",
                "WEBHOOK_SECRET_ARN": webhook_secret.secret_arn,
                # Console source viewer: read_source via the in-VPC proxy.
                "MCP_PROXY_FN": proxy_fn.function_name,
                # Per-repo Fargate service creation (lambdas/platform_api/runtimes.py).
                "ECS_CLUSTER": cluster.cluster_name,
                "TASK_IMAGE": image.image_uri,
                "TASK_ROLE_ARN": task_role.role_arn,
                "TASK_EXEC_ROLE_ARN": task_exec_role.role_arn,
                "TASK_SUBNETS": public_subnet_ids,
                "TASK_SECURITY_GROUP": service_sg.security_group_id,
                "CLOUDMAP_NAMESPACE_ID": namespace.namespace_id,
                "SERVICE_LOG_GROUP": mcp_log_group.log_group_name,
            },
        )
        platform_table.grant_read_write_data(platform_fn)
        table.grant_read_write_data(platform_fn)
        # files-source registration drops the uploads/<repo_id>/ folder marker;
        # the upload-management routes list, presign (POST policy signed by
        # this role's put permission) and delete objects under uploads/*.
        bucket.grant_put(platform_fn, "uploads/*")
        bucket.grant_read(platform_fn, "uploads/*")
        bucket.grant_delete(platform_fn, "uploads/*")
        # Graph explorer source viewer: the platform Lambda (not in the VPC)
        # reaches a repo's MCP task through the proxy Lambda, invoked directly
        # with a synthesized authorizer context after the platform's own
        # grant check (POST /repos/{id}/source forwards read_source only).
        proxy_fn.grant_invoke(platform_fn)
        # Graph explorer: head/read the published graph artifacts and mint
        # presigned GETs for viz.json / graph.json (GET /repos/{id}/graph).
        # Scoped to the graphify-out prefix — not the source snapshots or LLM
        # caches that also live under repos/<id>/.
        bucket.grant_read(platform_fn, "repos/*/latest/graphify-out/*")
        platform_fn.add_to_role_policy(
            iam.PolicyStatement(actions=["codebuild:StartBuild"], resources=[build_project.project_arn])
        )
        platform_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=[
                    "secretsmanager:CreateSecret",
                    "secretsmanager:PutSecretValue",
                    "secretsmanager:GetSecretValue",
                ],
                resources=[f"arn:aws:secretsmanager:{self.region}:{self.account}:secret:graphify/*"],
            )
        )
        webhook_secret.grant_read(platform_fn)
        # Per-repo Fargate service lifecycle. RegisterTaskDefinition and the
        # servicediscovery calls do not support resource-level scoping; the
        # ECS service mutations are pinned to this cluster's services.
        platform_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["ecs:RegisterTaskDefinition", "ecs:DescribeTaskDefinition"],
                resources=["*"],
            )
        )
        platform_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=[
                    "ecs:CreateService",
                    "ecs:UpdateService",
                    "ecs:DeleteService",
                    "ecs:DescribeServices",
                ],
                resources=[
                    f"arn:aws:ecs:{self.region}:{self.account}:service/{cluster.cluster_name}/*"
                ],
            )
        )
        platform_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=[
                    "servicediscovery:CreateService",
                    "servicediscovery:DeleteService",
                    "servicediscovery:GetService",
                    "servicediscovery:ListServices",
                    "servicediscovery:GetNamespace",
                    "servicediscovery:ListInstances",
                ],
                resources=["*"],
            )
        )
        platform_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["iam:PassRole"],
                resources=[task_role.role_arn, task_exec_role.role_arn],
                conditions={"StringEquals": {"iam:PassedToService": "ecs-tasks.amazonaws.com"}},
            )
        )
        platform_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["apigateway:POST"],
                resources=[
                    f"arn:aws:apigateway:{self.region}::/apikeys",
                    f"arn:aws:apigateway:{self.region}::/usageplans/{usage_plan.usage_plan_id}/keys",
                ],
            )
        )
        platform_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["apigateway:DELETE"],
                resources=[f"arn:aws:apigateway:{self.region}::/apikeys/*"],
            )
        )
        platform_fn.add_to_role_policy(
            iam.PolicyStatement(
                # ListUsers: members API + user search resolve email<->sub.
                # AdminListGroupsForUser/ResetUserPassword/DeleteUser: the admin
                # user-management console (list roles, reset a password, delete
                # a user).
                actions=["cognito-idp:AdminCreateUser", "cognito-idp:AdminAddUserToGroup",
                         "cognito-idp:ListUsers", "cognito-idp:AdminListGroupsForUser",
                         "cognito-idp:AdminResetUserPassword", "cognito-idp:AdminDeleteUser"],
                resources=[user_pool.user_pool_arn],
            )
        )

        # ------------------------------------------------------------------
        # Playground: Bedrock Claude (Anthropic SDK) + MCP bridge. MCP access
        # is the console identity: the Lambda checks the caller's grant on the
        # chosen server (registry + platform tables) and invokes the in-VPC
        # proxy directly — no API key. Separate function from platform_fn
        # purely for packaging — it carries the vendored anthropic SDK (~18MB)
        # that nothing else needs.
        # ------------------------------------------------------------------
        # Claude models >= Sonnet 4.6 that run under the account's DEFAULT
        # Bedrock data-retention mode. The Claude 5 family (Fable/Sonnet/Opus 5)
        # is deliberately excluded: those require data_retention_mode
        # provider_data_share (prompts/outputs shared with the provider), a
        # governance opt-in this account has not made. If it is later enabled,
        # add "global.anthropic.claude-sonnet-5" / "…-opus-5" / "…-fable-5".
        playground_models = [
            "global.anthropic.claude-sonnet-4-6",
            "global.anthropic.claude-opus-4-6-v1",
            "global.anthropic.claude-opus-4-7",
            "global.anthropic.claude-opus-4-8",
        ]
        playground_fn = lambda_.Function(
            self,
            "PlaygroundFn",
            runtime=lambda_.Runtime.PYTHON_3_12,
            architecture=lambda_.Architecture.ARM_64,
            handler="handler.handler",
            code=lambda_.Code.from_asset("lambdas/playground"),
            timeout=Duration.seconds(28),  # HTTP API integration ceiling is 30s
            memory_size=512,
            environment={
                "MCP_PROXY_FN": proxy_fn.function_name,
                "REGISTRY_TABLE": table.table_name,
                "ALLOWED_MODELS": ",".join(playground_models),
                "DEFAULT_MODEL": playground_models[0],
                "PLATFORM_TABLE": platform_table.table_name,
                # Per-user daily Bedrock token ceiling (input+output).
                "DAILY_TOKEN_BUDGET": "20000000",
            },
        )
        # USAGE#PLAYGROUND#<sub> daily counters (budget check + metering) and
        # the USER#/REPO# grant rows that gate server access.
        platform_table.grant_read_write_data(playground_fn)
        table.grant_read_data(playground_fn)
        proxy_fn.grant_invoke(playground_fn)
        bedrock_invoke = iam.PolicyStatement(
            actions=["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
            resources=[
                # Cross-region (apac./global.) profiles resolve to foundation
                # models in other regions, so both ARN families are needed.
                "arn:aws:bedrock:*::foundation-model/anthropic.*",
                f"arn:aws:bedrock:*:{self.account}:inference-profile/*.anthropic.*",
            ],
        )
        playground_fn.add_to_role_policy(bedrock_invoke)

        # Streaming variant: HTTP API Lambda integrations are buffered, so SSE
        # token streaming needs a Function URL in RESPONSE_STREAM mode. A
        # Function URL cannot attach the Cognito authorizer — AuthType is NONE
        # and the Lambda itself verifies the Cognito ACCESS token (aws-jwt-
        # verify) before doing anything, so the auth gate is equivalent.
        playground_stream_fn = lambda_.Function(
            self,
            "PlaygroundStreamFn",
            runtime=lambda_.Runtime.NODEJS_22_X,
            architecture=lambda_.Architecture.ARM_64,
            handler="index.handler",
            code=lambda_.Code.from_asset("lambdas/playground_stream"),
            timeout=Duration.seconds(300),
            memory_size=512,
            # A streaming invocation parks a slot for the whole generation (up
            # to minutes). Cap it so a burst of playground requests can NEVER
            # drain the account concurrency pool that the data-plane authorizer
            # and proxy Lambdas draw from — the Function URL has no API GW route
            # throttle, so this reservation IS the blast-radius control.
            reserved_concurrent_executions=20,
            environment={
                "MCP_PROXY_FN": proxy_fn.function_name,
                "REGISTRY_TABLE": table.table_name,
                "ALLOWED_MODELS": ",".join(playground_models),
                "DEFAULT_MODEL": playground_models[0],
                "PLATFORM_TABLE": platform_table.table_name,
                "DAILY_TOKEN_BUDGET": "20000000",
                "USER_POOL_ID": user_pool.user_pool_id,
                "USER_POOL_CLIENT_ID": spa_client.user_pool_client_id,
            },
        )
        platform_table.grant_read_write_data(playground_stream_fn)
        table.grant_read_data(playground_stream_fn)
        proxy_fn.grant_invoke(playground_stream_fn)
        playground_stream_fn.add_to_role_policy(bedrock_invoke)
        # AWS_IAM (never NONE): a NONE Function URL is world-accessible and is
        # auto-remediated by account security tooling (the resource policy's
        # Principal "*" gets scoped away → 403). Instead CloudFront fronts the
        # URL with OAC and SigV4-signs each request; the function's resource
        # policy grants invoke to CloudFront alone (SourceArn = distribution).
        # CloudFront preserves response streaming from a Function URL origin.
        playground_stream_furl = playground_stream_fn.add_function_url(
            auth_type=lambda_.FunctionUrlAuthType.AWS_IAM,
            invoke_mode=lambda_.InvokeMode.RESPONSE_STREAM,
        )
        # A DEDICATED distribution (not a behavior on the console distribution):
        # OAC adds an invoke permission whose SourceArn is the distribution, so
        # distribution ⇄ permission is a dependency cycle. On the console
        # distribution — whose domain (console_url) is referenced all over the
        # stack — that cycle pulls in ~25 resources and fails synth. A stand-
        # alone distribution with nothing else pointing at it lets CDK resolve
        # the pair cleanly (the documented with_origin_access_control shape).
        # Cross-origin from the console, so the Lambda answers CORS/OPTIONS.
        # NB: x-amz-content-sha256 is a signing header CloudFront manages for
        # OAC — it CANNOT go in an OriginRequestPolicy allow-list (CloudFront
        # rejects x-amz-* there). The browser still sends the body's SHA-256 in
        # that header; CloudFront folds it into the SigV4 signature on its own
        # (Lambda rejects unsigned POST payloads). Only the app headers are
        # allow-listed here.
        stream_origin_request_policy = cloudfront.OriginRequestPolicy(
            self,
            "PlaygroundStreamOriginReqPolicy",
            header_behavior=cloudfront.OriginRequestHeaderBehavior.allow_list("x-graphify-id", "content-type"),
            query_string_behavior=cloudfront.OriginRequestQueryStringBehavior.none(),
            cookie_behavior=cloudfront.OriginRequestCookieBehavior.none(),
        )
        stream_distribution = cloudfront.Distribution(
            self,
            "PlaygroundStreamDistribution",
            default_behavior=cloudfront.BehaviorOptions(
                # read_timeout is the max idle gap CloudFront tolerates between
                # origin bytes (60s = the standard-distribution ceiling). The
                # Lambda also emits an SSE keepalive every 10s so a slow tool
                # round on a big repo never trips it — this is the backstop.
                origin=origins.FunctionUrlOrigin.with_origin_access_control(
                    playground_stream_furl,
                    read_timeout=Duration.seconds(60),
                    keepalive_timeout=Duration.seconds(60),
                ),
                viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                allowed_methods=cloudfront.AllowedMethods.ALLOW_ALL,
                cache_policy=cloudfront.CachePolicy.CACHING_DISABLED,
                origin_request_policy=stream_origin_request_policy,
            ),
            http_version=cloudfront.HttpVersion.HTTP2_AND_3,
            minimum_protocol_version=cloudfront.SecurityPolicyProtocol.TLS_V1_2_2021,
            comment="graphify playground streaming (Bedrock SSE via Function URL + OAC)",
        )
        playground_stream_fn.add_environment("CONSOLE_ORIGIN", console_url)
        # Function URLs created after Oct 2025 require BOTH InvokeFunctionUrl
        # (added by with_origin_access_control) AND InvokeFunction for the
        # CloudFront principal, or OAC calls fail with 403 AccessDeniedException.
        playground_stream_fn.add_permission(
            "CloudFrontInvokeFunction",
            principal=iam.ServicePrincipal("cloudfront.amazonaws.com"),
            action="lambda:InvokeFunction",
            source_arn=stream_distribution.distribution_arn,
        )
        playground_stream_url = f"https://{stream_distribution.distribution_domain_name}/"

        mgmt_api = apigwv2.HttpApi(
            self,
            "PlatformApi",
            description="graphify platform management API (Cognito JWT)",
            cors_preflight=apigwv2.CorsPreflightOptions(
                allow_origins=[console_url, "http://localhost:8787"],
                allow_methods=[
                    apigwv2.CorsHttpMethod.GET,
                    apigwv2.CorsHttpMethod.POST,
                    apigwv2.CorsHttpMethod.DELETE,
                ],
                allow_headers=["authorization", "content-type"],
                max_age=Duration.hours(1),
            ),
        )
        jwt_authorizer = apigwv2_authorizers.HttpUserPoolAuthorizer(
            "ConsoleJwtAuthorizer", user_pool, user_pool_clients=[spa_client]
        )
        platform_integration = apigwv2_integrations.HttpLambdaIntegration("PlatformIntegration", platform_fn)
        mgmt_api.add_routes(
            path="/{proxy+}",
            methods=[apigwv2.HttpMethod.GET, apigwv2.HttpMethod.POST, apigwv2.HttpMethod.DELETE],
            integration=platform_integration,
            authorizer=jwt_authorizer,
        )
        # Graph explorer presign routes peeled off the catch-all (exact paths
        # win) purely so they can carry their own RouteSettings throttle below
        # — same Lambda, same authorizer.
        throttled_routes = []
        for gx_path in ("/repos/{repoId}/graph", "/catalog/{repoId}/graph"):
            throttled_routes += mgmt_api.add_routes(
                path=gx_path,
                methods=[apigwv2.HttpMethod.GET],
                integration=platform_integration,
                authorizer=jwt_authorizer,
            )
        # Exact-path routes win over {proxy+}, so the playground peels its two
        # endpoints off the platform catch-all while sharing the JWT authorizer.
        playground_integration = apigwv2_integrations.HttpLambdaIntegration(
            "PlaygroundIntegration", playground_fn
        )
        for pg_path in ("/playground/chat", "/playground/mcp"):
            throttled_routes += mgmt_api.add_routes(
                path=pg_path,
                methods=[apigwv2.HttpMethod.POST],
                integration=playground_integration,
                authorizer=jwt_authorizer,
            )
        # Route-level throttle: chat calls hold a Lambda + a Bedrock generation
        # for many seconds each — cap the platform-wide concurrency blast radius
        # (the per-user cost ceiling is the Lambda's daily token budget).
        mgmt_stage = mgmt_api.default_stage.node.default_child
        mgmt_stage.add_property_override("RouteSettings", {
            "POST /playground/chat": {"ThrottlingRateLimit": 5, "ThrottlingBurstLimit": 10},
            "POST /playground/mcp": {"ThrottlingRateLimit": 20, "ThrottlingBurstLimit": 40},
            # Graph presigns: each mints a bearer URL for a ~1MB (bundle) to
            # 32MB (raw fallback) object — cap the platform-wide mint rate;
            # the per-user daily cap lives in the handler.
            "GET /repos/{repoId}/graph": {"ThrottlingRateLimit": 5, "ThrottlingBurstLimit": 10},
            "GET /catalog/{repoId}/graph": {"ThrottlingRateLimit": 5, "ThrottlingBurstLimit": 10},
        })
        # RouteSettings is validated against EXISTING routes: the stage update
        # must run after the routes it names are created, or the deploy fails
        # with "Unable to find Route by key" (and the rollback wedges).
        for route in throttled_routes:
            mgmt_stage.add_resource_dependency(route.node.default_child)

        # ------------------------------------------------------------------
        # Console deployment (static SPA + runtime config.json)
        # ------------------------------------------------------------------
        s3_deployment.BucketDeployment(
            self,
            "ConsoleDeployment",
            destination_bucket=console_bucket,
            sources=[
                s3_deployment.Source.asset("console"),
                s3_deployment.Source.json_data(
                    "config.json",
                    {
                        "region": self.region,
                        "userPoolId": user_pool.user_pool_id,
                        "clientId": spa_client.user_pool_client_id,
                        "cognitoDomain": pool_domain.base_url(),
                        "apiBase": mgmt_api.api_endpoint,
                        "mcpBase": mcp_base_url,
                        "playgroundModels": playground_models,
                        "playgroundStreamUrl": playground_stream_url,
                    },
                ),
            ],
            distribution=distribution,
            distribution_paths=["/*"],
            # No build step => no content-hashed filenames; a uniform short
            # TTL beats immutable-caching stale app shells for a year.
            cache_control=[s3_deployment.CacheControl.max_age(Duration.minutes(5))],
        )

        CfnOutput(self, "McpDataApiUrl", value=mcp_base_url)
        CfnOutput(self, "PlatformApiUrl", value=mgmt_api.api_endpoint)
        CfnOutput(self, "ConsoleUrl", value=console_url)
        CfnOutput(self, "UserPoolId", value=user_pool.user_pool_id)
        CfnOutput(self, "UserPoolClientId", value=spa_client.user_pool_client_id)
        CfnOutput(self, "CognitoDomainUrl", value=pool_domain.base_url())
        CfnOutput(self, "PlatformTableName", value=platform_table.table_name)
        CfnOutput(self, "UsagePlanId", value=usage_plan.usage_plan_id)
        CfnOutput(self, "PlaygroundStreamUrl", value=playground_stream_url)

        # ------------------------------------------------------------------
        # Outputs
        # ------------------------------------------------------------------
        CfnOutput(self, "GraphBucketName", value=bucket.bucket_name)
        CfnOutput(self, "RepoRegistryTable", value=table.table_name)
        CfnOutput(self, "GraphBuildProject", value=build_project.project_name)
        CfnOutput(self, "PollerFunction", value=poller.function_name)
        CfnOutput(self, "WebhookUrl", value=webhook_api.api_endpoint + "/")
        CfnOutput(self, "WebhookSecretArn", value=webhook_secret.secret_arn)
        # For dynamic per-repo Fargate service creation (scripts + platform API).
        CfnOutput(self, "EcsClusterName", value=cluster.cluster_name)
        CfnOutput(self, "TaskImageUri", value=image.image_uri)
        CfnOutput(self, "TaskRoleArn", value=task_role.role_arn)
        CfnOutput(self, "TaskExecRoleArn", value=task_exec_role.role_arn)
        CfnOutput(self, "TaskSubnets", value=public_subnet_ids)
        CfnOutput(self, "TaskSecurityGroup", value=service_sg.security_group_id)
        CfnOutput(self, "CloudMapNamespaceId", value=namespace.namespace_id)
        CfnOutput(self, "ServiceLogGroup", value=mcp_log_group.log_group_name)
        CfnOutput(self, "ServiceDnsSuffix", value=service_dns_suffix)
        CfnOutput(self, "HubServiceName", value=hub_service.service_name)

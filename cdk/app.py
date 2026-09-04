#!/usr/bin/env python3
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import aws_cdk as cdk

from graphify_stack import GraphifyMcpPlatformStack

app = cdk.App()
# The CloudFormation stack name. Override with `-c stack_name=...` or the
# GRAPHIFY_STACK_NAME env var (the operator scripts read the same variable) when
# an existing deployment was created under another name — renaming a live
# stack creates a second one instead of updating it.
stack_name = (
    app.node.try_get_context("stack_name")
    or os.environ.get("GRAPHIFY_STACK_NAME")
    or "GraphifyMcpPlatform"
)
GraphifyMcpPlatformStack(
    app,
    stack_name,
    env=cdk.Environment(
        account=os.environ.get("CDK_DEFAULT_ACCOUNT"),
        region=os.environ.get("CDK_DEFAULT_REGION", "ap-northeast-2"),
    ),
    description="graphify knowledge-graph MCP platform on ECS Fargate + S3/CodeBuild graph pipeline",
)
# Opt-in cdk-nag run (`npx cdk synth -c nag=true`): writes
# cdk.out/AwsSolutions-<stack name>-NagReport.csv. Off by default so a plain
# deploy is unchanged.
if app.node.try_get_context("nag"):
    from cdk_nag import AwsSolutionsChecks

    cdk.Aspects.of(app).add(AwsSolutionsChecks(verbose=True))
app.synth()

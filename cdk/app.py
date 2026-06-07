#!/usr/bin/env python3
import os
import aws_cdk as cdk
from cdk.blog_stack import BlogStack

app = cdk.App()

BlogStack(
    app,
    "BlogStack",
    env=cdk.Environment(
        account=os.environ.get("CDK_DEFAULT_ACCOUNT", "039148079989"),
        # ACM certs for CloudFront must be in us-east-1
        region="us-east-1",
    ),
)

app.synth()

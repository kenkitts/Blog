from aws_cdk import (
    Stack,
    Duration,
    CfnOutput,
    aws_s3 as s3,
    aws_cloudfront as cloudfront,
    aws_cloudfront_origins as origins,
    aws_certificatemanager as acm,
    aws_route53 as route53,
    aws_route53_targets as targets,
    aws_budgets as budgets,
    aws_iam as iam,
)
from constructs import Construct

DOMAIN_NAME = "kenkitts.com"
WWW_DOMAIN = f"www.{DOMAIN_NAME}"
GITHUB_ORG = "kenkitts"  # GitHub username
GITHUB_REPO = "Blog"     # Repo name


class BlogStack(Stack):
    def __init__(self, scope: Construct, id: str, **kwargs) -> None:
        super().__init__(scope, id, **kwargs)

        # --- Hosted Zone (existing) ---
        zone = route53.HostedZone.from_lookup(
            self, "Zone", domain_name=DOMAIN_NAME
        )

        # --- ACM Certificate (existing) ---
        certificate = acm.Certificate.from_certificate_arn(
            self,
            "Certificate",
            "arn:aws:acm:us-east-1:039148079989:certificate/b16fc4c2-5adb-4ba8-9341-d92e7c891e16",
        )

        # --- S3 Bucket (existing, private, OAC access only) ---
        site_bucket = s3.Bucket.from_bucket_name(
            self, "SiteBucket", DOMAIN_NAME
        )

        # --- CloudFront Function (URL rewrite for index.html) ---
        url_rewrite_function = cloudfront.Function(
            self,
            "UrlRewriteFunction",
            code=cloudfront.FunctionCode.from_inline(
                """
function handler(event) {
    var request = event.request;
    var uri = request.uri;

    if (uri.endsWith('/')) {
        request.uri += 'index.html';
    } else if (!uri.includes('.')) {
        request.uri += '/index.html';
    }

    return request;
}
"""
            ),
            runtime=cloudfront.FunctionRuntime.JS_2_0,
        )

        # --- CloudFront Distribution ---
        distribution = cloudfront.Distribution(
            self,
            "Distribution",
            default_behavior=cloudfront.BehaviorOptions(
                origin=origins.S3BucketOrigin.with_origin_access_control(
                    site_bucket
                ),
                viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                cache_policy=cloudfront.CachePolicy.CACHING_OPTIMIZED,
                response_headers_policy=cloudfront.ResponseHeadersPolicy.SECURITY_HEADERS,
                function_associations=[
                    cloudfront.FunctionAssociation(
                        function=url_rewrite_function,
                        event_type=cloudfront.FunctionEventType.VIEWER_REQUEST,
                    )
                ],
            ),
            domain_names=[DOMAIN_NAME, WWW_DOMAIN],
            certificate=certificate,
            default_root_object="index.html",
            error_responses=[
                cloudfront.ErrorResponse(
                    http_status=404,
                    response_page_path="/404.html",
                    response_http_status=404,
                    ttl=Duration.minutes(5),
                ),
                cloudfront.ErrorResponse(
                    http_status=403,
                    response_page_path="/404.html",
                    response_http_status=404,
                    ttl=Duration.minutes(5),
                ),
            ],
            enable_logging=True,
            minimum_protocol_version=cloudfront.SecurityPolicyProtocol.TLS_V1_2_2021,
        )

        # --- Route 53 Records ---
        # Apex (kenkitts.com)
        route53.ARecord(
            self,
            "ApexRecord",
            zone=zone,
            target=route53.RecordTarget.from_alias(
                targets.CloudFrontTarget(distribution)
            ),
        )

        # www redirect via same distribution
        route53.ARecord(
            self,
            "WwwRecord",
            zone=zone,
            record_name="www",
            target=route53.RecordTarget.from_alias(
                targets.CloudFrontTarget(distribution)
            ),
        )

        # --- GitHub Actions OIDC Provider + Deploy Role ---
        github_oidc_provider = iam.OpenIdConnectProvider(
            self,
            "GitHubOidc",
            url="https://token.actions.githubusercontent.com",
            client_ids=["sts.amazonaws.com"],
        )

        deploy_role = iam.Role(
            self,
            "GitHubDeployRole",
            assumed_by=iam.WebIdentityPrincipal(
                github_oidc_provider.open_id_connect_provider_arn,
                conditions={
                    "StringEquals": {
                        "token.actions.githubusercontent.com:aud": "sts.amazonaws.com",
                    },
                    "StringLike": {
                        "token.actions.githubusercontent.com:sub": f"repo:{GITHUB_ORG}/{GITHUB_REPO}:ref:refs/heads/main",
                    },
                },
            ),
            description="Role for GitHub Actions to deploy blog",
        )

        # Grant deploy role permissions to sync S3 and invalidate CloudFront
        site_bucket.grant_read_write(deploy_role)
        deploy_role.add_to_policy(
            iam.PolicyStatement(
                actions=["cloudfront:CreateInvalidation"],
                resources=[
                    f"arn:aws:cloudfront::{self.account}:distribution/{distribution.distribution_id}"
                ],
            )
        )

        # --- Budget Alarm ($5/month) ---
        budgets.CfnBudget(
            self,
            "MonthlyBudget",
            budget=budgets.CfnBudget.BudgetDataProperty(
                budget_type="COST",
                time_unit="MONTHLY",
                budget_limit=budgets.CfnBudget.SpendProperty(
                    amount=5, unit="USD"
                ),
                budget_name="blog-monthly-budget",
            ),
            notifications_with_subscribers=[
                budgets.CfnBudget.NotificationWithSubscribersProperty(
                    notification=budgets.CfnBudget.NotificationProperty(
                        comparison_operator="GREATER_THAN",
                        notification_type="ACTUAL",
                        threshold=80,
                        threshold_type="PERCENTAGE",
                    ),
                    subscribers=[
                        budgets.CfnBudget.SubscriberProperty(
                            address="ken@kenkitts.com",
                            subscription_type="EMAIL",
                        )
                    ],
                )
            ],
        )

        # --- Outputs ---
        CfnOutput(self, "BucketName", value=site_bucket.bucket_name)
        CfnOutput(self, "DistributionId", value=distribution.distribution_id)
        CfnOutput(self, "DistributionDomain", value=distribution.distribution_domain_name)
        CfnOutput(self, "DeployRoleArn", value=deploy_role.role_arn)

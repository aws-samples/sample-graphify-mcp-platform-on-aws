#!/usr/bin/env python3
"""Bootstrap (or reset) a platform console user.

The pool is invite-only; the first admin has to come from operator
credentials. Sets a permanent password directly (no temp-password email),
so it also works for headless test users.

Usage:
  uv run python scripts/create_platform_user.py --email you@example.com --admin
  uv run python scripts/create_platform_user.py --email t@example.com --password '...'  # explicit password
"""

from __future__ import annotations

import argparse
import secrets
import string
import sys

import boto3

from common import STACK_NAME, stack_outputs


def gen_password() -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(20)) + "!Aa1"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--email", required=True)
    ap.add_argument("--password", default="", help="permanent password (generated when omitted)")
    ap.add_argument("--admin", action="store_true", help="add to the admin group (else member)")
    ap.add_argument("--region", default="ap-northeast-2")
    ap.add_argument("--stack", default=STACK_NAME)
    args = ap.parse_args()

    outputs = stack_outputs(args.region, args.stack)
    pool_id = outputs["UserPoolId"]
    idp = boto3.client("cognito-idp", region_name=args.region)
    password = args.password or gen_password()

    try:
        idp.admin_create_user(
            UserPoolId=pool_id,
            Username=args.email,
            UserAttributes=[
                {"Name": "email", "Value": args.email},
                {"Name": "email_verified", "Value": "true"},
            ],
            MessageAction="SUPPRESS",
        )
        print(f"created {args.email}")
    except idp.exceptions.UsernameExistsException:
        print(f"{args.email} already exists; resetting password")
    idp.admin_set_user_password(UserPoolId=pool_id, Username=args.email, Password=password, Permanent=True)
    group = "admin" if args.admin else "member"
    idp.admin_add_user_to_group(UserPoolId=pool_id, Username=args.email, GroupName=group)

    print(f"group   : {group}")
    print(f"password: {password}")
    print(f"console : {outputs.get('ConsoleUrl', '?')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

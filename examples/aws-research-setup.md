# AWS read-only setup for the research env

Goal: give the research agent a credential that can **read** your AWS account
but cannot change anything — then expose it as the `research-readonly` profile
that [`research-access.yaml`](../research-access.yaml.example) points at.

The policy itself is in [`iam-research-policy.json`](iam-research-policy.json):
broad read across common services, an explicit deny on sensitive reads
(Secrets Manager, KMS decrypt, SSM parameters), and a blanket deny on every
write verb. This file is the *how to use it* that the k8s example
([`kubectl-readonly-rbac.yaml`](kubectl-readonly-rbac.yaml)) has inline.

**Prerequisites:** an AWS account, the `aws` CLI installed on your host, and
permission to create IAM policies and an IAM identity (console or CLI). If your
org uses IAM Identity Center (SSO) rather than IAM users, skip to
[Option B](#option-b--sso--assume-role-no-long-lived-keys).

---

## 1. Create the policy

**Console:** IAM → Policies → **Create policy** → **JSON** tab → paste the
contents of [`iam-research-policy.json`](iam-research-policy.json) → name it
`leash-research-readonly`.

**CLI:**

```bash
aws iam create-policy \
  --policy-name leash-research-readonly \
  --policy-document file://examples/iam-research-policy.json
# note the returned Arn — you attach it below
```

---

## 2. Create an identity and attach the policy

### Option A — dedicated IAM user (simplest)

Best for a single-person setup. You get a long-lived access key pair; rotate it
on a schedule like the GitHub PATs.

```bash
ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
POLICY_ARN="arn:aws:iam::${ACCOUNT_ID}:policy/leash-research-readonly"

aws iam create-user --user-name leash-research
aws iam attach-user-policy --user-name leash-research --policy-arn "$POLICY_ARN"
aws iam create-access-key --user-name leash-research
# → copy AccessKeyId and SecretAccessKey from the output; the secret is shown ONCE
```

(Console equivalent: IAM → Users → **Create user** `leash-research`, no console
access → attach `leash-research-readonly` → after creation, **Security
credentials** → **Create access key** → "Application running outside AWS".)

### Option B — SSO / assume-role (no long-lived keys)

Stronger, and the right choice if your org uses IAM Identity Center. Create a
**role** (not a user) with `leash-research-readonly` attached, set its trust
policy to whoever assumes it, then configure a profile that assumes it:

```ini
# ~/.aws/config
[profile research-readonly]
role_arn       = arn:aws:iam::<ACCOUNT_ID>:role/leash-research-readonly
source_profile = default          # an existing profile allowed to assume it
region         = us-east-1
```

Skip step 3 if you use this — the profile is already defined here.

---

## 3. Configure the `research-readonly` profile (Option A only)

`research-access.yaml` sets `AWS_PROFILE: research-readonly`, so the profile
name must match exactly.

```bash
aws configure --profile research-readonly
# AWS Access Key ID     → the AccessKeyId from step 2
# AWS Secret Access Key → the SecretAccessKey from step 2
# Default region name   → us-east-1   (or yours)
# Default output format  → json
```

This writes the key pair to `~/.aws/credentials` and the region to
`~/.aws/config`. The harness mounts your whole `~/.aws` directory read-only into
the research container, so the profile is visible there with no token text ever
entering the workspace.

---

## 4. Wire it into research-access.yaml

Already shown in [`research-access.yaml.example`](../research-access.yaml.example),
reproduced here for completeness:

```yaml
platforms:
  - name: aws
    install: aws-cli            # catalog Feature: installs the CLI + AWS egress
    credential: ~/.aws          # mounted read-only
    env:
      AWS_PROFILE: research-readonly
      AWS_REGION: us-east-1
```

Then `scripts/setup.sh` → **Rebuild Container**.

---

## 5. Verify on the host before opening the container

Mirror the read-works / write-is-denied check the k8s example uses:

```bash
# a read should work:
AWS_PROFILE=research-readonly aws sts get-caller-identity      # returns the identity
AWS_PROFILE=research-readonly aws s3 ls                        # lists buckets

# a write should be denied:
AWS_PROFILE=research-readonly aws ec2 create-tags \
  --resources i-0123456789abcdef0 --tags Key=x,Value=y
# → expected: An error occurred (UnauthorizedOperation / AccessDenied)

# a sensitive read should also be denied:
AWS_PROFILE=research-readonly aws secretsmanager get-secret-value --secret-id any
# → expected: AccessDenied (explicit deny in the policy)
```

If a write succeeds, **stop** — the profile is using the wrong identity or the
policy didn't attach. Read-only is yours to guarantee; the harness only mounts
the credential read-only on disk, it can't prove the credential itself can't
write.

---

## Rotation

For Option A, treat the access key like the GitHub PATs: rotate on a schedule,
and on suspected leak run `aws iam delete-access-key --user-name leash-research
--access-key-id <old>`, create a new one, re-run `aws configure --profile
research-readonly`. Option B's assumed-role credentials are short-lived, so
there's nothing to rotate.

#!/bin/bash
# One-shot, ephemeral benchmark run on AWS: creates everything, runs `maef bench` on a fresh EC2 instance, downloads
# the results, then deletes everything it created (on success, failure or Ctrl-C). Nothing is left running.
#
#   OPENROUTER_API_KEY=... AWS_PROFILE=maef scripts/aws-bench/run.sh
#
# Settings (environment): AWS_REGION (default eu-north-1), RUNS ("seed:noise ...", default 9 runs over noise 0-2),
# INSTANCE_TYPE (t3.small), OUT (bench-results/aws-<timestamp>). Needs the AWS CLI and git; the commit at HEAD is
# what runs. Creates, all tagged Project=maef and named maef-bench-<id>: an IAM role + instance profile (SSM core +
# access to the bucket and the key only), a security group with no inbound rules, a private S3 bucket, an SSM
# SecureString holding the OpenRouter key, and one instance (IMDSv2, encrypted disk, self-terminates after 4 h).
set -euo pipefail
: "${OPENROUTER_API_KEY:?export OPENROUTER_API_KEY first}"
export AWS_REGION=${AWS_REGION:-eu-north-1} AWS_PAGER=""
RUNS=${RUNS:-"1:0 2:0 3:0 3:1 4:1 5:1 7:2 8:2 9:2"}
TYPE=${INSTANCE_TYPE:-t3.small}
HERE=$(cd "$(dirname "$0")" && pwd); REPO=$(git -C "$HERE" rev-parse --show-toplevel)
ID=$(date -u +%Y%m%d-%H%M%S); NAME=maef-bench-$ID
OUT=${OUT:-$REPO/bench-results/aws-$ID}; mkdir -p "$OUT"
ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
BUCKET=$NAME-$ACCOUNT; PARAM=/maef-bench/$ID/openrouter-api-key
TAGS="Key=Project,Value=maef Key=Name,Value=$NAME"
WORK=$(mktemp -d); INSTANCE=""; SG=""
log() { printf '[%s] %s\n' "$(date +%T)" "$*"; }

cleanup() {
  set +e; log "cleanup: deleting everything created by this run"
  if [ -n "$INSTANCE" ]; then
    aws ec2 terminate-instances --instance-ids "$INSTANCE" >/dev/null
    aws ec2 wait instance-terminated --instance-ids "$INSTANCE"
  fi
  if [ -n "$SG" ]; then  # the instance's network interface can take a moment to detach
    for i in 1 2 3 4 5 6; do aws ec2 delete-security-group --group-id "$SG" 2>/dev/null && break; sleep 10; done
  fi
  aws s3 rm --recursive --quiet "s3://$BUCKET" 2>/dev/null; aws s3api delete-bucket --bucket "$BUCKET" 2>/dev/null
  aws ssm delete-parameter --name "$PARAM" 2>/dev/null
  aws iam delete-role-policy --role-name "$NAME" --policy-name bench-access 2>/dev/null
  aws iam detach-role-policy --role-name "$NAME" \
    --policy-arn arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore 2>/dev/null
  aws iam remove-role-from-instance-profile --instance-profile-name "$NAME" --role-name "$NAME" 2>/dev/null
  aws iam delete-instance-profile --instance-profile-name "$NAME" 2>/dev/null
  aws iam delete-role --role-name "$NAME" 2>/dev/null
  rm -rf "$WORK"
  left=$(aws resourcegroupstaggingapi get-resources --tag-filters "Key=Name,Values=$NAME" \
         --query 'ResourceTagMappingList[].ResourceARN' --output text 2>/dev/null)
  log "cleanup done${left:+ (tag index may lag; listed: $left)}"
}
trap cleanup EXIT

# --- create ---
log "bundle HEAD $(git -C "$REPO" rev-parse --short HEAD)"
git -C "$REPO" bundle create "$WORK/maef.bundle" HEAD main 2>/dev/null

log "IAM role and instance profile $NAME"
aws iam create-role --role-name "$NAME" --tags $TAGS --assume-role-policy-document \
  '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"ec2.amazonaws.com"},"Action":"sts:AssumeRole"}]}' >/dev/null
aws iam attach-role-policy --role-name "$NAME" --policy-arn arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore
aws iam put-role-policy --role-name "$NAME" --policy-name bench-access --policy-document "{\"Version\":\"2012-10-17\",\"Statement\":[
  {\"Effect\":\"Allow\",\"Action\":[\"s3:GetObject\",\"s3:PutObject\"],\"Resource\":\"arn:aws:s3:::$BUCKET/*\"},
  {\"Effect\":\"Allow\",\"Action\":\"ssm:GetParameter\",\"Resource\":\"arn:aws:ssm:$AWS_REGION:$ACCOUNT:parameter$PARAM\"}]}"
aws iam create-instance-profile --instance-profile-name "$NAME" --tags $TAGS >/dev/null
aws iam add-role-to-instance-profile --instance-profile-name "$NAME" --role-name "$NAME"

log "bucket $BUCKET (private) and inputs"
aws s3api create-bucket --bucket "$BUCKET" --create-bucket-configuration "LocationConstraint=$AWS_REGION" >/dev/null
aws s3api put-public-access-block --bucket "$BUCKET" --public-access-block-configuration \
  BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
for f in "$WORK/maef.bundle" "$HERE/vm-setup.sh" "$HERE/vm-bench.sh"; do
  aws s3 cp --only-show-errors "$f" "s3://$BUCKET/in/$(basename "$f")"
done

log "OpenRouter key -> SSM SecureString $PARAM"
( umask 077; printf '%s' "$OPENROUTER_API_KEY" > "$WORK/key" )
aws ssm put-parameter --name "$PARAM" --type SecureString --value "file://$WORK/key" --tags $TAGS >/dev/null
rm -f "$WORK/key"

log "security group (no inbound)"
VPC=$(aws ec2 describe-vpcs --filters Name=is-default,Values=true --query 'Vpcs[0].VpcId' --output text)
SG=$(aws ec2 create-security-group --group-name "$NAME" --description "maef bench: no inbound" --vpc-id "$VPC" \
     --tag-specifications "ResourceType=security-group,Tags=[{Key=Project,Value=maef},{Key=Name,Value=$NAME}]" \
     --query GroupId --output text)

AMI=$(aws ssm get-parameter --query Parameter.Value --output text \
      --name /aws/service/canonical/ubuntu/server/24.04/stable/current/amd64/hvm/ebs-gp3/ami-id)
log "launch $TYPE ($AMI)"
for attempt in 1 2 3 4 5 6; do  # a new instance profile takes a few seconds to become usable
  INSTANCE=$(aws ec2 run-instances --image-id "$AMI" --instance-type "$TYPE" --count 1 \
    --iam-instance-profile "Name=$NAME" --security-group-ids "$SG" --associate-public-ip-address \
    --credit-specification CpuCredits=standard --instance-initiated-shutdown-behavior terminate \
    --metadata-options HttpTokens=required,HttpEndpoint=enabled,HttpPutResponseHopLimit=2 \
    --block-device-mappings '[{"DeviceName":"/dev/sda1","Ebs":{"VolumeSize":20,"VolumeType":"gp3","Encrypted":true,"DeleteOnTermination":true}}]' \
    --user-data "file://$HERE/userdata.sh" \
    --tag-specifications "ResourceType=instance,Tags=[{Key=Project,Value=maef},{Key=Name,Value=$NAME}]" \
                         "ResourceType=volume,Tags=[{Key=Project,Value=maef},{Key=Name,Value=$NAME}]" \
    --query 'Instances[0].InstanceId' --output text 2>/dev/null) && break
  INSTANCE=""; sleep 10
done
[ -n "$INSTANCE" ] || { log "launch failed"; exit 1; }
log "instance $INSTANCE"

# --- run ---
ssm_run() {  # ssm_run <timeout seconds> <command> -> prints stdout; fails unless the command succeeded
  local cid status
  cid=$(aws ssm send-command --instance-ids "$INSTANCE" --document-name AWS-RunShellScript \
        --parameters "$(python3 -c 'import json,sys; print(json.dumps({"commands":[sys.argv[2]],"executionTimeout":[sys.argv[1]]}))' "$1" "$2")" \
        --query Command.CommandId --output text)
  while :; do
    sleep 15
    status=$(aws ssm get-command-invocation --command-id "$cid" --instance-id "$INSTANCE" --query Status --output text 2>/dev/null || echo Pending)
    case $status in Success|Failed|TimedOut|Cancelled) break ;; esac
  done
  aws ssm get-command-invocation --command-id "$cid" --instance-id "$INSTANCE" --query StandardOutputContent --output text
  [ "$status" = Success ]
}

log "waiting for SSM and bootstrap"
until [ "$(aws ssm describe-instance-information --filters "Key=InstanceIds,Values=$INSTANCE" \
          --query 'InstanceInformationList[0].PingStatus' --output text 2>/dev/null)" = Online ]; do sleep 15; done
until ssm_run 60 "test -f /var/lib/cloud/maef-bootstrap-done" >/dev/null 2>&1; do
  ssm_run 60 "cloud-init status" 2>/dev/null | grep -q error && { log "bootstrap failed"; exit 1; }
done

AS_USER="sudo -iu ubuntu env XDG_RUNTIME_DIR=/run/user/1000 DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus"
log "setup and container preflight"
ssm_run 1800 "$AS_USER bash -c 'aws s3 cp --only-show-errors s3://$BUCKET/in/vm-setup.sh ~/vm-setup.sh && aws s3 cp --only-show-errors s3://$BUCKET/in/vm-bench.sh ~/vm-bench.sh && BUNDLE=s3://$BUCKET/in/maef.bundle bash ~/vm-setup.sh 2>&1'" \
  | tee "$OUT/setup.log" | tail -4
grep -q SETUP_OK "$OUT/setup.log" || { log "setup failed, see $OUT/setup.log"; exit 1; }

log "bench: $RUNS"
ssm_run 7200 "$AS_USER bash -c 'KEY_PARAM=$PARAM AWS_REGION=$AWS_REGION RUNS=\"$RUNS\" RESULTS=s3://$BUCKET/out/results.tgz bash ~/vm-bench.sh 2>&1'" \
  | tee "$OUT/bench.log" | grep -E "^== bench|Status:|Verdict|BENCH_DONE" || true

# --- fetch ---
aws s3 cp --only-show-errors "s3://$BUCKET/out/results.tgz" "$OUT/results.tgz"
tar -C "$OUT" -xzf "$OUT/results.tgz"
log "results in $OUT/data (reports: $OUT/data/bench-results/*/report.md)"

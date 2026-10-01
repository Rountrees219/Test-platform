#!/usr/bin/env bash
# BASH_INFRUSTRUCTURE_SETUP.sh — Provision a minimal, internet-reachable AWS VPC.
#
# Creates a VPC with a public subnet, an internet gateway, a route table with a
# 0.0.0.0/0 route to that gateway (associated with the subnet), a security
# group, and optional CloudWatch monitoring. A JSON summary of the created
# resources is written to the backup directory.
#
# Usage:
#   ./BASH_INFRUSTRUCTURE_SETUP.sh --region us-east-1
#   ./BASH_INFRUSTRUCTURE_SETUP.sh --region us-east-1 --name ninja --cidr 10.0.0.0/16
#   ENABLE_MONITORING=1 ./BASH_INFRUSTRUCTURE_SETUP.sh --region us-east-1
#
# Exit codes:
#   0  success
#   1  usage / missing prerequisite (aws cli, region)
#   2  an AWS API call failed
#
# Requirements: awscli v2, credentials with EC2 (and CloudWatch when monitoring
# is enabled) permissions, and a region (via --region or AWS_REGION).

set -Eeuo pipefail

# ---------------------------------------------------------------------------
# Config / defaults
# ---------------------------------------------------------------------------

REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-}}"
VPC_CIDR="${VPC_CIDR:-10.0.0.0/16}"
SUBNET_CIDR="${SUBNET_CIDR:-10.0.1.0/24}"
NAME="${NAME:-ninja}"
ENABLE_MONITORING="${ENABLE_MONITORING:-0}"
BACKUP_DIR="${BACKUP_DIR:-./backups}"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

log()  { printf '\033[1;34m▶\033[0m %s\n' "$*"; }
ok()   { printf '\033[1;32m✓\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m✗\033[0m %s\n' "$*" >&2; exit "${2:-1}"; }

usage() {
    cat <<EOF
Usage: $0 --region <aws-region> [options]

Options:
  --region REGION        AWS region (required unless AWS_REGION is set)
  --name NAME            Resource name prefix (default: ${NAME})
  --cidr CIDR            VPC CIDR block (default: ${VPC_CIDR})
  --subnet-cidr CIDR     Subnet CIDR block (default: ${SUBNET_CIDR})
  --backup-dir DIR       Where to write the JSON summary (default: ${BACKUP_DIR})
  --monitoring           Create CloudWatch monitoring resources
  --help                 Show this help

Environment:
  AWS_REGION / AWS_DEFAULT_REGION   Region fallback
  ENABLE_MONITORING=1               Same as --monitoring
  VPC_CIDR, SUBNET_CIDR, NAME, BACKUP_DIR   Override defaults
EOF
}

# ---------------------------------------------------------------------------
# Parse arguments
# ---------------------------------------------------------------------------

while [[ $# -gt 0 ]]; do
    case "$1" in
        --region)      REGION="$2"; shift 2 ;;
        --name)        NAME="$2"; shift 2 ;;
        --cidr)        VPC_CIDR="$2"; shift 2 ;;
        --subnet-cidr) SUBNET_CIDR="$2"; shift 2 ;;
        --backup-dir)  BACKUP_DIR="$2"; shift 2 ;;
        --monitoring)  ENABLE_MONITORING=1; shift ;;
        --help|-h)     usage; exit 0 ;;
        *)             usage >&2; die "unknown argument: $1" 1 ;;
    esac
done

# ---------------------------------------------------------------------------
# Prerequisite checks (issue #5): aws cli present + region configured
# ---------------------------------------------------------------------------

if ! command -v aws >/dev/null 2>&1; then
    die "AWS CLI not found on PATH. Install awscli v2 and retry." 1
fi

if [[ -z "$REGION" ]]; then
    die "No region configured. Pass --region <aws-region> or export AWS_REGION." 1
fi

export AWS_REGION="$REGION"
export AWS_DEFAULT_REGION="$REGION"

if ! aws sts get-caller-identity >/dev/null 2>&1; then
    die "AWS credentials are not valid for region ${REGION}." 2
fi
ok "Prerequisites OK (aws cli, region ${REGION})"

# ---------------------------------------------------------------------------
# VPC
# ---------------------------------------------------------------------------

log "Creating VPC ${VPC_CIDR} (${NAME})..."
VPC_ID="$(aws ec2 create-vpc \
    --cidr-block "$VPC_CIDR" \
    --tag-specifications "ResourceType=vpc,Tags=[{Key=Name,Value=${NAME}-vpc}]" \
    --query 'Vpc.VpcId' --output text)" \
    || die "Failed to create VPC" 2

[[ -n "$VPC_ID" && "$VPC_ID" != "None" ]] || die "VPC creation returned no id" 2
aws ec2 modify-vpc-attribute --vpc-id "$VPC_ID" --enable-dns-hostnames >/dev/null
ok "VPC: ${VPC_ID}"

# ---------------------------------------------------------------------------
# Subnet
# ---------------------------------------------------------------------------

log "Creating subnet ${SUBNET_CIDR}..."
SUBNET_ID="$(aws ec2 create-subnet \
    --vpc-id "$VPC_ID" \
    --cidr-block "$SUBNET_CIDR" \
    --tag-specifications "ResourceType=subnet,Tags=[{Key=Name,Value=${NAME}-subnet}]" \
    --query 'Subnet.SubnetId' --output text)" \
    || die "Failed to create subnet" 2

[[ -n "$SUBNET_ID" && "$SUBNET_ID" != "None" ]] || die "Subnet creation returned no id" 2

# Public subnet: auto-assign a public IPv4 address on launch.
aws ec2 modify-subnet-attribute \
    --subnet-id "$SUBNET_ID" \
    --map-public-ip-on-launch >/dev/null
ok "Subnet: ${SUBNET_ID}"

# ---------------------------------------------------------------------------
# Internet gateway (issue #3: validate creation, fail fast)
# ---------------------------------------------------------------------------

log "Creating internet gateway..."
IGW_ID="$(aws ec2 create-internet-gateway \
    --tag-specifications "ResourceType=internet-gateway,Tags=[{Key=Name,Value=${NAME}-igw}]" \
    --query 'InternetGateway.InternetGatewayId' --output text)" \
    || die "Failed to create internet gateway" 2

if [[ -z "$IGW_ID" || "$IGW_ID" == "None" ]]; then
    die "Internet gateway creation returned no id" 2
fi

aws ec2 attach-internet-gateway \
    --internet-gateway-id "$IGW_ID" \
    --vpc-id "$VPC_ID" \
    || die "Failed to attach internet gateway ${IGW_ID} to VPC ${VPC_ID}" 2
ok "Internet gateway: ${IGW_ID} (attached)"

# ---------------------------------------------------------------------------
# Route table + default route + subnet association (issue #1, HIGH)
# Without this the subnet has no path to the internet gateway.
# ---------------------------------------------------------------------------

log "Creating route table and default route..."
ROUTE_TABLE_ID="$(aws ec2 create-route-table \
    --vpc-id "$VPC_ID" \
    --tag-specifications "ResourceType=route-table,Tags=[{Key=Name,Value=${NAME}-rtb}]" \
    --query 'RouteTable.RouteTableId' --output text)" \
    || die "Failed to create route table" 2

[[ -n "$ROUTE_TABLE_ID" && "$ROUTE_TABLE_ID" != "None" ]] \
    || die "Route table creation returned no id" 2

aws ec2 create-route \
    --route-table-id "$ROUTE_TABLE_ID" \
    --destination-cidr-block 0.0.0.0/0 \
    --gateway-id "$IGW_ID" >/dev/null \
    || die "Failed to add default route to ${IGW_ID}" 2

ROUTE_ASSOC_ID="$(aws ec2 associate-route-table \
    --subnet-id "$SUBNET_ID" \
    --route-table-id "$ROUTE_TABLE_ID" \
    --query 'AssociationId' --output text)" \
    || die "Failed to associate route table ${ROUTE_TABLE_ID} with subnet ${SUBNET_ID}" 2

[[ -n "$ROUTE_ASSOC_ID" && "$ROUTE_ASSOC_ID" != "None" ]] \
    || die "Route table association returned no id" 2
ok "Route table: ${ROUTE_TABLE_ID} → 0.0.0.0/0 via ${IGW_ID} (assoc ${ROUTE_ASSOC_ID})"

# ---------------------------------------------------------------------------
# Security group
# ---------------------------------------------------------------------------

log "Creating security group..."
SG_ID="$(aws ec2 create-security-group \
    --group-name "${NAME}-sg" \
    --description "Security group for ${NAME}" \
    --vpc-id "$VPC_ID" \
    --query 'GroupId' --output text)" \
    || die "Failed to create security group" 2

[[ -n "$SG_ID" && "$SG_ID" != "None" ]] || die "Security group creation returned no id" 2
ok "Security group: ${SG_ID}"

# ---------------------------------------------------------------------------
# CloudWatch monitoring (issue #4: real implementation, opt-in)
# Creates a log group and a dashboard summarising the provisioned resources.
# ---------------------------------------------------------------------------

MONITORING_STATUS="disabled"
if [[ "$ENABLE_MONITORING" == "1" ]]; then
    log "Setting up CloudWatch monitoring..."
    LOG_GROUP="/aws/vpc/${NAME}"
    if aws logs create-log-group --log-group-name "$LOG_GROUP" 2>/dev/null; then
        ok "CloudWatch log group: ${LOG_GROUP}"
    else
        warn "CloudWatch log group ${LOG_GROUP} already exists or could not be created"
    fi

    DASHBOARD_BODY="$(printf '{"widgets":[{"type":"text","x":0,"y":0,"width":24,"height":6,"properties":{"markdown":"# %s\\n\\n- VPC: %s\\n- Subnet: %s\\n- IGW: %s\\n- Route table: %s\\n- Security group: %s\\n- Region: %s"}}]}' \
        "$NAME" "$VPC_ID" "$SUBNET_ID" "$IGW_ID" "$ROUTE_TABLE_ID" "$SG_ID" "$REGION")"
    if aws cloudwatch put-dashboard \
        --dashboard-name "${NAME}-vpc" \
        --dashboard-body "$DASHBOARD_BODY" >/dev/null 2>&1; then
        ok "CloudWatch dashboard: ${NAME}-vpc"
    else
        warn "Could not create CloudWatch dashboard"
    fi
    MONITORING_STATUS="enabled"
fi

# ---------------------------------------------------------------------------
# Backup config JSON (issue #2: well-formed, timestamp properly quoted)
# ---------------------------------------------------------------------------

mkdir -p "$BACKUP_DIR"
TIMESTAMP="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
BACKUP_FILE="${BACKUP_DIR}/${NAME}-vpc-${TIMESTAMP//:/-}.json"

if command -v jq >/dev/null 2>&1; then
    jq -n \
        --arg name "$NAME" \
        --arg region "$REGION" \
        --arg vpc_id "$VPC_ID" \
        --arg subnet_id "$SUBNET_ID" \
        --arg igw_id "$IGW_ID" \
        --arg route_table_id "$ROUTE_TABLE_ID" \
        --arg route_assoc_id "$ROUTE_ASSOC_ID" \
        --arg sg_id "$SG_ID" \
        --arg vpc_cidr "$VPC_CIDR" \
        --arg subnet_cidr "$SUBNET_CIDR" \
        --arg monitoring "$MONITORING_STATUS" \
        --arg timestamp "$TIMESTAMP" \
        '{name:$name, region:$region, vpc_id:$vpc_id, subnet_id:$subnet_id,
          igw_id:$igw_id, route_table_id:$route_table_id,
          route_association_id:$route_assoc_id, security_group_id:$sg_id,
          vpc_cidr:$vpc_cidr, subnet_cidr:$subnet_cidr,
          monitoring:$monitoring, timestamp:$timestamp}' > "$BACKUP_FILE"
else
    # Fallback writer: every string value — including the timestamp — is quoted.
    cat > "$BACKUP_FILE" <<JSON
{
  "name": "${NAME}",
  "region": "${REGION}",
  "vpc_id": "${VPC_ID}",
  "subnet_id": "${SUBNET_ID}",
  "igw_id": "${IGW_ID}",
  "route_table_id": "${ROUTE_TABLE_ID}",
  "route_association_id": "${ROUTE_ASSOC_ID}",
  "security_group_id": "${SG_ID}",
  "vpc_cidr": "${VPC_CIDR}",
  "subnet_cidr": "${SUBNET_CIDR}",
  "monitoring": "${MONITORING_STATUS}",
  "timestamp": "${TIMESTAMP}"
}
JSON
fi

# Validate the JSON we just wrote (best effort).
if command -v jq >/dev/null 2>&1; then
    jq -e . "$BACKUP_FILE" >/dev/null || die "Generated backup JSON is malformed: ${BACKUP_FILE}" 2
fi
ok "Backup config: ${BACKUP_FILE}"

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

cat <<EOF

Infrastructure ready in ${REGION}:
  VPC               ${VPC_ID}
  Subnet            ${SUBNET_ID}  (public, route table ${ROUTE_TABLE_ID})
  Internet gateway  ${IGW_ID}
  Route table       ${ROUTE_TABLE_ID}  0.0.0.0/0 → ${IGW_ID}
  Security group    ${SG_ID}
  Monitoring        ${MONITORING_STATUS}
  Backup            ${BACKUP_FILE}
EOF

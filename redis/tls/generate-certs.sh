#!/bin/bash
# Generate self-signed TLS certificates for Redis (development use only).
# For staging/production, use certificates issued by your organization's CA.
#
# Usage: ./generate-certs.sh [output_dir]
# Default output: current directory

set -euo pipefail

OUTPUT_DIR="${1:-.}"
mkdir -p "$OUTPUT_DIR"

DAYS=3650
SUBJ_CA="/C=US/ST=Dev/L=Local/O=Elitea/OU=Dev/CN=Redis-CA"
SUBJ_SERVER="/C=US/ST=Dev/L=Local/O=Elitea/OU=Dev/CN=redis"
SUBJ_CLIENT="/C=US/ST=Dev/L=Local/O=Elitea/OU=Dev/CN=redis-client"

echo "=== Generating Redis TLS certificates (self-signed, dev only) ==="

# CA key and certificate
openssl genrsa -out "$OUTPUT_DIR/ca.key" 4096
openssl req -x509 -new -nodes \
    -key "$OUTPUT_DIR/ca.key" \
    -sha256 -days "$DAYS" \
    -out "$OUTPUT_DIR/ca.crt" \
    -subj "$SUBJ_CA"

# Server key and certificate
openssl genrsa -out "$OUTPUT_DIR/redis.key" 2048
openssl req -new \
    -key "$OUTPUT_DIR/redis.key" \
    -out "$OUTPUT_DIR/redis.csr" \
    -subj "$SUBJ_SERVER"

# Server SAN config (redis hostname and localhost for dev)
cat > "$OUTPUT_DIR/server-ext.cnf" <<EOF
authorityKeyIdentifier=keyid,issuer
basicConstraints=CA:FALSE
keyUsage=digitalSignature,keyEncipherment
extendedKeyUsage=serverAuth
subjectAltName=DNS:redis,DNS:localhost,IP:127.0.0.1
EOF

openssl x509 -req \
    -in "$OUTPUT_DIR/redis.csr" \
    -CA "$OUTPUT_DIR/ca.crt" \
    -CAkey "$OUTPUT_DIR/ca.key" \
    -CAcreateserial \
    -out "$OUTPUT_DIR/redis.crt" \
    -days "$DAYS" \
    -sha256 \
    -extfile "$OUTPUT_DIR/server-ext.cnf"

# Client key and certificate (for mTLS if needed)
openssl genrsa -out "$OUTPUT_DIR/client.key" 2048
openssl req -new \
    -key "$OUTPUT_DIR/client.key" \
    -out "$OUTPUT_DIR/client.csr" \
    -subj "$SUBJ_CLIENT"

cat > "$OUTPUT_DIR/client-ext.cnf" <<EOF
authorityKeyIdentifier=keyid,issuer
basicConstraints=CA:FALSE
keyUsage=digitalSignature,keyEncipherment
extendedKeyUsage=clientAuth
EOF

openssl x509 -req \
    -in "$OUTPUT_DIR/client.csr" \
    -CA "$OUTPUT_DIR/ca.crt" \
    -CAkey "$OUTPUT_DIR/ca.key" \
    -CAcreateserial \
    -out "$OUTPUT_DIR/client.crt" \
    -days "$DAYS" \
    -sha256 \
    -extfile "$OUTPUT_DIR/client-ext.cnf"

# Set appropriate permissions
chmod 600 "$OUTPUT_DIR"/*.key
chmod 644 "$OUTPUT_DIR"/*.crt

# Cleanup CSR and extension files
rm -f "$OUTPUT_DIR"/*.csr "$OUTPUT_DIR"/*.cnf "$OUTPUT_DIR"/*.srl

echo "=== Certificates generated in $OUTPUT_DIR ==="
echo "  CA:     ca.crt, ca.key"
echo "  Server: redis.crt, redis.key"
echo "  Client: client.crt, client.key"
echo ""
echo "To verify: openssl verify -CAfile $OUTPUT_DIR/ca.crt $OUTPUT_DIR/redis.crt"

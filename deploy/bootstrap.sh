#!/bin/bash
# One-time bootstrap of the platform after backend hosts are provisioned and
# Aurora clusters + Redis are reachable. Run from ONE backend host.
#
# Required environment:
#   APP_DOMAIN                  - apex domain, e.g. "example.com"
#   BOOTSTRAP_ADMIN_PASSWORD    - initial tenant_admin password (record it!)

set -e

cd /home/ubuntu/tenants_back
source venv/bin/activate

echo "=== Step 1: PostGIS extension on every Aurora cluster ==="
for alias in default tenant_1 tenant_2; do
    python manage.py dbshell --database=$alias <<EOF
        CREATE EXTENSION IF NOT EXISTS postgis;
EOF
done

echo "=== Step 2: SHARED_APPS migrations on default.public ==="
python manage.py migrate_schemas --shared --database=default

echo "=== Step 3: Populate Shard table from settings.DATABASES ==="
python manage.py sync_shards --activate

echo "=== Step 4: Create public Tenant + Domain ==="
python manage.py shell <<EOF
from tenants.models import Tenant, Domain, Shard
from django_tenants.utils import get_public_schema_name

if not Tenant.objects.filter(schema_name=get_public_schema_name()).exists():
    pub = Tenant(
        schema_name=get_public_schema_name(),
        name="Public",
        shard=Shard.objects.get(is_default=True),
        status=Tenant.Status.ACTIVE,
    )
    pub.full_clean()
    pub.save()
    Domain.objects.create(domain="${APP_DOMAIN}", tenant=pub, is_primary=True)
    print("OK - public tenant + domain created.")
else:
    print("-- public tenant already exists, skipping.")
EOF

echo "=== Step 5: Create the first tenant_admin user ==="
python manage.py shell <<EOF
from users.models import User
import os
if not User.objects.filter(username="admin").exists():
    User.objects.create_superuser(
        username="admin",
        email="admin@${APP_DOMAIN}",
        password=os.environ["BOOTSTRAP_ADMIN_PASSWORD"],
        role=User.Role.TENANT_ADMIN,
    )
    print("OK - admin user created (role=tenant_admin).")
else:
    print("-- admin user already exists, skipping.")
EOF

echo ""
echo "Bootstrap complete."
echo "Log in at https://${APP_DOMAIN}/admin/ as 'admin'"
echo "(use the password from BOOTSTRAP_ADMIN_PASSWORD - record it in your secret store)."

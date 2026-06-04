#!/bin/bash
# Cloud-init / user-data for each backend EC2.
# Runs once on first boot. Expects the following environment variables to be
# available via the EC2 launch template, plus the following SSM parameters
# (fetched below; the EC2 IAM role must allow ssm:GetParameter for them):
#
# Env vars from launch template:
#   DEFAULT_DB_HOST, TENANT_1_DB_HOST, TENANT_2_DB_HOST   (Aurora endpoints)
#   REDIS_APP_HOST     (ElastiCache cluster for app cache + sessions)
#
# SSM SecureString parameters (one per Aurora cluster):
#   /tenants/django_secret
#   /tenants/default/db_user      /tenants/default/db_password
#   /tenants/tenant_1/db_user     /tenants/tenant_1/db_password
#   /tenants/tenant_2/db_user     /tenants/tenant_2/db_password
#
# Note: the AWS RDS CA bundle is vendored in the repo at
# `deploy/certs/aws-rds-global-bundle.pem`, so we do NOT download it here.
# `settings.py` defaults AWS_RDS_CA to that vendored path.

set -e

# System packages: nginx, Python 3.12, GIS native libs, supervisor.
apt-get update
apt-get install -y nginx python3.12 python3.12-venv python3-pip git supervisor \
    binutils libproj-dev gdal-bin libgeos-dev

# Service user: the default 'ubuntu' user already exists on the AMI, so we
# do not create a dedicated one for the MVP.

# Clone code and create the virtualenv. (The run/ socket dir is created after
# the clone, below — git clone requires an empty target directory.)
sudo -u ubuntu git clone https://github.com/yourorg/tenants_back.git /home/ubuntu/tenants_back
sudo -u ubuntu python3.12 -m venv /home/ubuntu/tenants_back/venv
sudo -u ubuntu /home/ubuntu/tenants_back/venv/bin/pip \
    install -r /home/ubuntu/tenants_back/requirements.txt

# Pull secrets from SSM Parameter Store - one credential pair per Aurora cluster.
DJANGO_SECRET=$(aws ssm get-parameter --name /tenants/django_secret --with-decryption --query Parameter.Value --output text)

DEFAULT_DB_USER=$(aws ssm     get-parameter --name /tenants/default/db_user                       --query Parameter.Value --output text)
DEFAULT_DB_PASSWORD=$(aws ssm get-parameter --name /tenants/default/db_password  --with-decryption --query Parameter.Value --output text)

TENANT_1_DB_USER=$(aws ssm     get-parameter --name /tenants/tenant_1/db_user                      --query Parameter.Value --output text)
TENANT_1_DB_PASSWORD=$(aws ssm get-parameter --name /tenants/tenant_1/db_password --with-decryption --query Parameter.Value --output text)

TENANT_2_DB_USER=$(aws ssm     get-parameter --name /tenants/tenant_2/db_user                      --query Parameter.Value --output text)
TENANT_2_DB_PASSWORD=$(aws ssm get-parameter --name /tenants/tenant_2/db_password --with-decryption --query Parameter.Value --output text)

# Generate settings_local.py with production hosts and secrets.
cat > /home/ubuntu/tenants_back/tenants_back/settings_local.py <<EOF
from .settings import DATABASES, CACHES, _aurora_db_options

DEBUG = False
SECRET_KEY = "${DJANGO_SECRET}"
ALLOWED_HOSTS = [".example.com", "example.com"]

SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
USE_X_FORWARDED_HOST = True
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
SESSION_COOKIE_DOMAIN = ".example.com"
CSRF_COOKIE_DOMAIN    = ".example.com"
CSRF_TRUSTED_ORIGINS  = ["https://example.com", "https://*.example.com"]

CORS_ALLOW_ALL_ORIGINS = False
CORS_ALLOWED_ORIGIN_REGEXES = [
    r"^https://([a-z0-9-]+\.)?example\.com(:\d+)?$",
]

_AURORA_DEFAULTS = {
    "ENGINE":             "django_tenants.postgresql_backend",
    "NAME":               "tenants_back",
    "PORT":               "5432",
    "CONN_MAX_AGE":       60,
    "CONN_HEALTH_CHECKS": True,
    "OPTIONS":            _aurora_db_options(),
}

DATABASES["default"] = {
    **_AURORA_DEFAULTS,
    "HOST":     "${DEFAULT_DB_HOST}",
    "USER":     "${DEFAULT_DB_USER}",
    "PASSWORD": "${DEFAULT_DB_PASSWORD}",
}
DATABASES["tenant_1"] = {
    **_AURORA_DEFAULTS,
    "HOST":     "${TENANT_1_DB_HOST}",
    "USER":     "${TENANT_1_DB_USER}",
    "PASSWORD": "${TENANT_1_DB_PASSWORD}",
}
DATABASES["tenant_2"] = {
    **_AURORA_DEFAULTS,
    "HOST":     "${TENANT_2_DB_HOST}",
    "USER":     "${TENANT_2_DB_USER}",
    "PASSWORD": "${TENANT_2_DB_PASSWORD}",
}

CACHES["default"]["LOCATION"] = "redis://${REDIS_APP_HOST}:6379/0"
EOF

# Generate static files locally (served by nginx directly).
sudo -u ubuntu /home/ubuntu/tenants_back/venv/bin/python \
    /home/ubuntu/tenants_back/manage.py collectstatic --noinput

# Wire up nginx.
cp /home/ubuntu/tenants_back/deploy/nginx_backend.conf /etc/nginx/sites-available/tenants.conf
ln -sf /etc/nginx/sites-available/tenants.conf /etc/nginx/sites-enabled/
rm -f /etc/nginx/sites-enabled/default
# nginx (www-data) reaches the gunicorn socket directly: gunicorn runs with
# --group=www-data (see bin/gunicorn_start.sh), so the socket is group-owned by
# www-data. Create the run dir owned by the service user, group www-data, with
# setgid + group-traversable — no need to add www-data to the ubuntu group.
install -d -o ubuntu -g www-data -m 2750 /home/ubuntu/tenants_back/run
systemctl restart nginx

# Wire up supervisor + gunicorn.
cp /home/ubuntu/tenants_back/deploy/supervisor_gunicorn.conf /etc/supervisor/conf.d/gunicorn.conf
chmod +x /home/ubuntu/tenants_back/bin/gunicorn_start.sh
supervisorctl reread
supervisorctl update
supervisorctl start gunicorn

#!/bin/bash
# MAEF bench VM bootstrap (Ubuntu 24.04). Cost guard: the instance powers off (and, with
# shutdown behaviour "terminate", is deleted) 4 hours after boot no matter what.
set -euxo pipefail
shutdown -h +240 "maef bench VM: 4 hour limit"
export DEBIAN_FRONTEND=noninteractive
apt-get update -q
apt-get install -y -q podman uidmap slirp4netns passt fuse-overlayfs python3-venv python3-pip git
snap install aws-cli --classic
loginctl enable-linger ubuntu
touch /var/lib/cloud/maef-bootstrap-done

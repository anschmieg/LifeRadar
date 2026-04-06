#!/bin/bash
#
# Fix Docker Runtime on Oracle Cloud Ubuntu 24.04
# Addresses: OCI runtime create failed - sysctl permission denied
#

set -e

echo "=========================================="
echo "Docker Runtime Fix for Oracle Cloud"
echo "=========================================="
echo ""

# Check if running as root
if [[ $EUID -ne 0 ]]; then
   echo "This script must be run as root (use: sudo bash fix-docker-runtime.sh)"
   exit 1
fi

echo "Step 1: Checking current kernel version..."
uname -r

echo ""
echo "Step 2: Creating Docker daemon override..."
mkdir -p /etc/docker

# Fix 1: Configure Docker to use userland-proxy and disable iptables modifications
cat > /etc/docker/daemon.json << 'DOCKERCONFIG'
{
  "userland-proxy": true,
  "ip-forward": false,
  "iptables": false,
  "ip6tables": false,
  "experimental": false,
  "default-ulimits": {
    "nofile": {
      "Name": "nofile",
      "Hard": 64000,
      "Soft": 64000
    }
  }
}
DOCKERCONFIG

echo "Docker daemon configuration updated."

echo ""
echo "Step 3: Checking AppArmor status..."
if command -v aa-status &> /dev/null; then
    echo "AppArmor is installed. Checking docker-default profile..."
    
    # Update AppArmor docker-default profile to allow sysctl access
    cat > /etc/apparmor.d/local/docker << 'APPCONFIG'
# Allow sysctl access for containers
/proc/sys/net/ipv4/ip_unprivileged_port_start r,
/sys/fs/cgroup/system.slice/docker-*.scope/** r,
APPCONFIG
    
    # Reload AppArmor
    apparmor_parser -r /etc/apparmor.d/docker || true
    echo "AppArmor profile updated."
else
    echo "AppArmor not found, skipping..."
fi

echo ""
echo "Step 4: Configuring sysctl for unprivileged containers..."
# These settings help with container permissions
cat > /etc/sysctl.d/99-docker-fix.conf << 'SYSCTL'
# Allow unprivileged users to create user namespaces
kernel.unprivileged_userns_clone=1
# Increase inotify limits
fs.inotify.max_user_watches=524288
fs.inotify.max_user_instances=512
SYSCTL

# Apply sysctl settings
sysctl --system

echo ""
echo "Step 5: Restarting Docker service..."
systemctl daemon-reload
systemctl restart docker

# Wait for Docker to be ready
sleep 3

echo ""
echo "Step 6: Testing Docker..."
if docker run --rm hello-world 2>&1 | grep -q "Hello from Docker"; then
    echo "✅ SUCCESS: Docker is working!"
else
    echo "❌ FAILED: Docker test failed. Checking alternative fixes..."
    echo ""
    echo "Attempting Fix #2: Disabling user namespaces..."
    
    # Alternative: Disable user namespace remapping
    cat > /etc/docker/daemon.json << 'DOCKERCONFIG2'
{
  "userns-remap": "default",
  "userland-proxy": true
}
DOCKERCONFIG2
    
    systemctl restart docker
    sleep 3
    
    if docker run --rm hello-world 2>&1 | grep -q "Hello from Docker"; then
        echo "✅ SUCCESS: Docker is working with userns-remap!"
    else
        echo "❌ FAILED: Docker still not working. Trying nuclear option..."
        echo ""
        echo "Attempting Fix #3: Minimal Docker config..."
        
        # Nuclear option: Minimal config
        echo '{}' > /etc/docker/daemon.json
        systemctl restart docker
        sleep 3
        
        if docker run --rm hello-world 2>&1 | grep -q "Hello from Docker"; then
            echo "✅ SUCCESS: Docker is working with minimal config!"
        else
            echo "❌ All fixes failed. Manual intervention required."
            echo ""
            echo "Suggested next steps:"
            echo "1. Check kernel logs: dmesg | tail -50"
            echo "2. Try older kernel: apt install linux-image-6.8.0-1017-oracle"
            echo "3. Review: journalctl -u docker -n 100"
            exit 1
        fi
    fi
fi

echo ""
echo "=========================================="
echo "Docker Runtime Fix Complete"
echo "=========================================="
echo ""
echo "Docker status:"
docker ps

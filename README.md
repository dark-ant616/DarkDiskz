# DarkDiskz

DarkDiskz is an open-source, Linux-native disk management GUI focused on:

- Creating Bcache setups (HDD + SSD/NVMe caching)
- Simple RAID0 and RAID1
- Disk health monitoring and SMART tests
- Clean, beginner-friendly GTK4 interface

**Main Features:**
- Show all disks with model, serial, interface, capacity, health
- Run SMART quick and long tests
- Create and manage Bcache devices
- Create and manage simple RAID arrays
- Auto-generate fstab entries
- Modern, simple UI

**License:** GPLv3

## Getting Started

### Dependencies
Full Installation Requirements
System Packages (Ubuntu/Debian)
Install these with sudo apt install ...:
python3 (Python 3.8+)
python3-gi (Python GObject Introspection bindings)
python3-gi-cairo (for some graphics support)
gir1.2-gtk-4.0 (GTK4 bindings)
gir1.2-adw-1 (libadwaita bindings)
bcache-tools (for bcache support)
mdadm (for RAID support)
smartmontools (for SMART/drive health)
wipefs (for drive wiping)
lsb-release (for system info)
lshw (for hardware info, optional)
lsblk (should be present by default)
lspci (for GPU info, optional)
udev (should be present by default)
xterm or gnome-terminal or another terminal emulator (for privileged commands)

Python Packages (install with pip)
PyGObject>=3.40
pyudev>=0.22

pip install -r requirements.txt



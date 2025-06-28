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

- Python 3.10+
- PyGObject (GTK4)
- bcache-tools
- mdadm
- smartmontools
- lsblk, udevadm

Install requirements:

```bash
sudo apt install python3-gi gir1.2-gtk-4.0 bcache-tools mdadm smartmontools



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
- Disk read/Write benchmarking

**License:** GPLv3
![image](https://github.com/user-attachments/assets/f008f01a-b2d5-4d8b-a115-e5f5e2c00618)


## Getting Started
- chmod +x install_deps.sh
./install_deps.sh
- pip install -r requirements.txt
  
## Usage

- All destructive actions (format, wipe, RAID creation) require confirmation and will prompt for your password in a terminal window.
- The app is designed to be safe, but **always back up your data** before making changes to drives.

## Contributing

Contributions, bug reports, and feature requests are welcome!  
Open an issue or submit a pull request on GitHub.

---

## Troubleshooting

- If you see errors about missing GTK or Adwaita, make sure you installed all system dependencies.
- If you have issues with hotplug detection, ensure `pyudev` is installed and your user has appropriate permissions.
- For advanced troubleshooting, run the app from a terminal to see debug output.

---
![image](https://github.com/user-attachments/assets/0de26a47-5815-45b8-9ea6-e8d14bdb4675)


## Credits

Created by dark_ant.  
Inspired by the need for a modern, open-source Linux disk management tool.

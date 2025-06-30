#!/usr/bin/env python3
import gi
import subprocess
import json
import threading
import os
from pathlib import Path
import platform
import shutil
import shlex
import datetime
import json as pyjson
import glob

# --- GTK/Adw Imports ---
# These require PyGObject and libadwaita (python3-gi, gir1.2-gtk-4.0, gir1.2-adw-1)
import gi
try:
    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
except (ImportError, ValueError):
    pass  # Let the app fail later if not installed
from gi.repository import Gtk, Gio, GLib, Adw  # type: ignore

# --- USB Hotplug Support (pyudev) ---
try:
    import pyudev  # type: ignore
except ImportError:
    pyudev = None  # Optional dependency for real-time USB detection


class DriveInfo:
    """Simple drive information collector"""
    
    @staticmethod
    def get_drives():
        """Get list of storage drives"""
        try:
            result = subprocess.run([
                "lsblk", "-d", "-J", "-o", 
                "NAME,SIZE,MODEL,TRAN,ROTA,TYPE,HOTPLUG,RM"
            ], capture_output=True, text=True, check=True)
            
            data = json.loads(result.stdout)
            drives = []
            
            for device in data.get('blockdevices', []):
                if device.get('type') == 'disk':
                    drives.append({
                        'name': f"/dev/{device['name']}",
                        'display_name': device.get('name', 'Unknown'),
                        'size': device.get('size', 'Unknown'),
                        'model': device.get('model', 'Unknown Drive'),
                        'transport': device.get('tran', 'Unknown'),
                        'rotational': device.get('rota', False),
                        'is_nvme': 'nvme' in device.get('name', '').lower(),
                        'is_removable': device.get('rm', False),
                        'is_hotplug': device.get('hotplug', False),
                        'is_usb': device.get('tran', '').lower() == 'usb'
                    })
            
            return drives
        except Exception as e:
            print(f"Error getting drives: {e}")
            return []
    
    @staticmethod
    def filter_drives(drives, show_usb=True):
        """Filter drives based on type - now only filters true USB drives"""
        main_drives = []
        usb_drives = []
        
        for drive in drives:
            # Only filter out true USB drives (not just removable)
            if drive['is_usb'] and drive['transport'].lower() == 'usb':
                usb_drives.append(drive)
            else:
                main_drives.append(drive)
        
        return main_drives, usb_drives
    
    @staticmethod
    def check_smartctl_available():
        """Check if smartctl is available"""
        try:
            subprocess.run(["which", "smartctl"], check=True, capture_output=True)
            return True
        except subprocess.CalledProcessError:
            return False
    
    @staticmethod
    def get_smart_info(device):
        """Get SMART/NVMe info, including all attributes as a list of dicts."""
        import re
        if not DriveInfo.check_smartctl_available():
            return {'available': False, 'output': 'smartctl not found', 'health': None, 'error': 'smartctl not installed'}
        try:
            # Try without sudo first
            result = subprocess.run([
                "smartctl", "-H", "-A", device
            ], capture_output=True, text=True)
            output = result.stdout
            if result.returncode in [0, 4]:  # 0 = success, 4 = some SMART errors
                health = 'PASSED' in output
                # Parse attributes
                attributes = []
                # For classic SMART (ATA/SATA)
                smart_attr_re = re.compile(r"^\s*(\d+)\s+([\w\-_]+)\s+(\w+)\s+(\d+)\s+(\d+)\s+(\d+)\s+([\w\-_]+)\s+([\w\-_]+)\s+([\w\-_]+)\s+([\w\-_]+)\s+(.+)$")
                in_table = False
                for line in output.splitlines():
                    if line.strip().startswith('ID#') or line.strip().startswith('ID'):  # Table header
                        in_table = True
                        continue
                    if in_table and line.strip() == '':
                        in_table = False
                    if in_table:
                        parts = line.split()
                        if len(parts) >= 10 and parts[0].isdigit():
                            attr = {
                                'id': parts[0],
                                'name': parts[1],
                                'value': parts[3],
                                'worst': parts[4],
                                'thresh': parts[5],
                                'type': parts[6],
                                'updated': parts[7],
                                'when_failed': parts[8],
                                'raw': ' '.join(parts[9:]),
                            }
                            attributes.append(attr)
                # For NVMe, parse Vendor Specific SMART Log
                if not attributes:
                    nvme_section = False
                    for line in output.splitlines():
                        if 'SMART/Health Information' in line:
                            nvme_section = True
                        elif nvme_section and line.strip() == '':
                            break
                        elif nvme_section and ':' in line:
                            k, v = line.split(':', 1)
                            attributes.append({'name': k.strip(), 'value': v.strip(), 'raw': v.strip()})
                return {
                    'available': True,
                    'output': output,
                    'health': health,
                    'needs_sudo': False,
                    'attributes': attributes
                }
            elif result.returncode == 1 and "Permission denied" in result.stderr:
                # Try with sudo
                try:
                    result = subprocess.run([
                        "sudo", "smartctl", "-H", "-A", device
                    ], capture_output=True, text=True)
                    output = result.stdout
                    if result.returncode in [0, 4]:
                        # Parse as above
                        # ... (repeat parsing logic) ...
                        return DriveInfo.get_smart_info(device)  # Fallback: re-call without sudo
                except Exception as e:
                    return {'available': False, 'output': f'Sudo failed: {e}', 'health': None, 'error': str(e)}
            return {
                'available': False, 
                'output': f'Command failed: {result.stderr}', 
                'health': None,
                'error': f'smartctl returned code {result.returncode}'
            }
        except Exception as e:
            return {'available': False, 'output': f'Error: {e}', 'health': None, 'error': str(e)}
    
    @staticmethod
    def run_smart_test(device, test_type):
        """Always launch the terminal for sudo smartctl tests."""
        import os
        if not DriveInfo.check_smartctl_available():
            return False, "smartctl not found"
        env = os.environ.copy()
        env['LANG'] = 'C'
        term = DriveInfo.detect_terminal()
        if not term:
            return False, "No supported terminal emulator found. Please install gnome-terminal, xterm, or similar."
        smart_cmd = f"sudo smartctl -t {shlex.quote(test_type)} {shlex.quote(device)}; read -n 1 -s -r -p 'Press any key to close...'"
        if "gnome-terminal" in term or "xfce4-terminal" in term or "lxterminal" in term:
            cmd = [term, "--", "bash", "-c", smart_cmd]
        elif "konsole" in term:
            cmd = [term, "-e", "bash", "-c", smart_cmd]
        else:
            cmd = [term, "-e", "bash", "-c", smart_cmd]
        try:
            subprocess.run(cmd, check=True)
        except Exception as e:
            return False, f"Failed to launch terminal: {e}"
        # After terminal closes, re-check SMART status
        return True, "Test started (check SMART status after a few seconds)"

    @staticmethod
    def get_percent_used(device):
        # Try to get the percentage used for the drive (first partition or mountpoint)
        import subprocess, json
        try:
            # Use lsblk to get children (partitions)
            result = subprocess.run([
                "lsblk", "-J", "-o", "NAME,MOUNTPOINT,SIZE,FSTYPE,TYPE", device
            ], capture_output=True, text=True, check=True)
            data = json.loads(result.stdout)
            devs = data.get('blockdevices', [])
            if not devs:
                return None
            dev = devs[0]
            # Find first mounted partition
            for child in dev.get('children', []):
                if child.get('mountpoint'):
                    # Use df to get percent used
                    df = subprocess.run(["df", "-P", child['mountpoint']], capture_output=True, text=True)
                    lines = df.stdout.splitlines()
                    if len(lines) >= 2:
                        percent = lines[1].split()[4].replace('%','')
                        return percent
            return None
        except Exception:
            return None

    @staticmethod
    def parse_smart_temperature(output):
        # This method should be implemented to parse temperature information from the SMART output
        # For now, it returns None as the implementation is not provided
        return None

    @staticmethod
    def get_technical_details(device):
        """Return a dict of technical details for the device: rotation, trim, link speed, partition table, RAID/Bcache membership, RPM, NVMe gen."""
        import subprocess, json
        details = {}
        try:
            # Get lsblk -d -J -o NAME,ROTA,DISC-ALN,DISC-GRAN,DISC-MAX,DISC-ZERO,PTTYPE,TRAN,RM,MODEL
            result = subprocess.run([
                "lsblk", "-d", "-J", "-o",
                "NAME,ROTA,DISC-ALN,DISC-GRAN,DISC-MAX,DISC-ZERO,PTTYPE,TRAN,RM,MODEL"
            ], capture_output=True, text=True, check=True)
            data = json.loads(result.stdout)
            devname = device.replace("/dev/", "")
            for dev in data.get('blockdevices', []):
                if dev.get('name') == devname:
                    details['rotation'] = dev.get('rota')
                    details['partition_table'] = dev.get('pttype')
                    details['transport'] = dev.get('tran')
                    details['removable'] = dev.get('rm')
                    details['model'] = dev.get('model')
                    break
            # Get TRIM support (lsblk -D)
            trim_result = subprocess.run(["lsblk", "-D", "-J", device], capture_output=True, text=True)
            trim_data = json.loads(trim_result.stdout)
            if trim_data.get('blockdevices'):
                details['trim'] = trim_data['blockdevices'][0].get('discard', None)
            # Get udevadm info for more details
            try:
                udevadm = subprocess.run(["udevadm", "info", "--query=all", "--name", device], capture_output=True, text=True)
                for line in udevadm.stdout.splitlines():
                    if 'ID_ATA_ROTATION_RATE_RPM' in line:
                        details['rpm'] = line.split('=')[1]
                    if 'ID_NVME_PCI_SUBSYS' in line:
                        details['nvme_pci'] = line.split('=')[1]
                    if 'ID_ATA_SPEED' in line or 'ID_NVME_PCI_LINK_SPEED' in line:
                        details['link_speed'] = line.split('=')[1]
            except Exception:
                pass
            # NVMe/SSD/HDD logic
            is_nvme = 'nvme' in devname.lower() or (details.get('transport', '').lower() == 'nvme')
            is_rotational = details.get('rotation')
            # TRIM: always supported for NVMe
            if is_nvme:
                details['trim'] = 'Supported'
            # Rotation speed
            if is_rotational:
                if details.get('rpm') and details['rpm'] != '0':
                    details['rotation_speed'] = f"{details['rpm']} rpm"
                else:
                    details['rotation_speed'] = 'Unknown (HDD)'
            elif is_nvme:
                # NVMe: PCIe Gen
                gen = 'Unknown'
                if details.get('link_speed'):
                    if '8.0' in details['link_speed']:
                        gen = 'PCIe Gen 3'
                    elif '16.0' in details['link_speed']:
                        gen = 'PCIe Gen 4'
                    elif '32.0' in details['link_speed']:
                        gen = 'PCIe Gen 5'
                details['rotation_speed'] = gen + ' (NVMe)'
            else:
                details['rotation_speed'] = 'Solid State (SSD)'
            # RAID/Bcache membership (simple check)
            details['in_raid'] = False
            details['in_bcache'] = False
            # Check mdadm
            try:
                with open('/proc/mdstat') as f:
                    if devname in f.read():
                        details['in_raid'] = True
            except Exception:
                pass
            # Check bcache
            try:
                if subprocess.run(["ls", f"/sys/block/{devname}/bcache"], capture_output=True).returncode == 0:
                    details['in_bcache'] = True
            except Exception:
                pass
        except Exception as e:
            details['error'] = str(e)
        return details

    @staticmethod
    def get_partitions(device):
        """Return a list of dicts: name, fs, mountpoint, size, used% for each partition."""
        import subprocess, json
        partitions = []
        try:
            result = subprocess.run([
                "lsblk", "-J", "-o", "NAME,MOUNTPOINT,SIZE,FSTYPE,TYPE", device
            ], capture_output=True, text=True, check=True)
            data = json.loads(result.stdout)
            devs = data.get('blockdevices', [])
            if not devs:
                return []
            dev = devs[0]
            for child in dev.get('children', []):
                part = {
                    'name': child.get('name'),
                    'size': child.get('size'),
                    'fstype': child.get('fstype'),
                    'mountpoint': child.get('mountpoint'),
                    'used': None
                }
                if child.get('mountpoint'):
                    df = subprocess.run(["df", "-P", child['mountpoint']], capture_output=True, text=True)
                    lines = df.stdout.splitlines()
                    if len(lines) >= 2:
                        part['used'] = lines[1].split()[4]
                partitions.append(part)
        except Exception:
            pass
        return partitions

    @staticmethod
    def detect_terminal():
        for term in ["gnome-terminal", "x-terminal-emulator", "xterm", "konsole", "xfce4-terminal", "lxterminal"]:
            if shutil.which(term):
                return term
        return None


class BcacheManager:
    """Bcache management functionality"""
    
    @staticmethod
    def check_bcache_available():
        """Check if bcache tools are available"""
        try:
            subprocess.run(["which", "make-bcache"], check=True, capture_output=True)
            return True
        except subprocess.CalledProcessError:
            return False
    
    @staticmethod
    def get_bcache_devices():
        """Get list of bcache devices"""
        try:
            result = subprocess.run([
                "lsblk", "-J", "-o", "NAME,TYPE,SIZE,MOUNTPOINT"
            ], capture_output=True, text=True, check=True)
            
            data = json.loads(result.stdout)
            bcache_devices = []
            
            for device in data.get('blockdevices', []):
                if 'bcache' in device.get('name', '').lower():
                    bcache_devices.append({
                        'name': device['name'],
                        'size': device.get('size', 'Unknown'),
                        'mountpoint': device.get('mountpoint', 'Not mounted')
                    })
            
            return bcache_devices
        except Exception as e:
            print(f"Error getting bcache devices: {e}")
            return []
    
    @staticmethod
    def build_bcache_command(backing_device, cache_device=None):
        """Return the make-bcache command as a list for use in a terminal."""
        if cache_device:
            return ["sudo", "make-bcache", "-B", backing_device, "-C", cache_device]
        else:
            return ["sudo", "make-bcache", "-B", backing_device]


class RaidManager:
    """RAID management functionality"""
    
    @staticmethod
    def check_mdadm_available():
        """Check if mdadm is available"""
        try:
            subprocess.run(["which", "mdadm"], check=True, capture_output=True)
            return True
        except subprocess.CalledProcessError:
            return False
    
    @staticmethod
    def get_raid_arrays():
        """Get list of RAID arrays"""
        try:
            result = subprocess.run([
                "cat", "/proc/mdstat"
            ], capture_output=True, text=True, check=True)
            
            # Parse mdstat output (simplified)
            arrays = []
            lines = result.stdout.split('\n')
            
            for line in lines:
                if line.startswith('md'):
                    parts = line.split()
                    if len(parts) >= 4:
                        arrays.append({
                            'name': f"/dev/{parts[0]}",
                            'level': parts[3] if len(parts) > 3 else 'unknown',
                            'status': 'active' if 'active' in line else 'inactive',
                            'devices': parts[4:] if len(parts) > 4 else []
                        })
            
            return arrays
        except Exception as e:
            print(f"Error getting RAID arrays: {e}")
            return []
    
    @staticmethod
    def create_raid(level, devices, array_name):
        """Create RAID array"""
        try:
            cmd = [
                "sudo", "mdadm", "--create", f"/dev/{array_name}",
                "--level", str(level), "--raid-devices", str(len(devices))
            ] + devices
            
            result = subprocess.run(cmd, capture_output=True, text=True)
            return result.returncode == 0, result.stdout if result.returncode == 0 else result.stderr
        except Exception as e:
            return False, str(e)


class BcacheWizard(Adw.Window):
    """Step-by-step Bcache setup wizard"""
    def __init__(self, parent):
        super().__init__()
        self.set_transient_for(parent)
        self.set_modal(True)
        self.set_default_size(600, 400)
        self.set_title("Bcache Setup Wizard")
        self.current_step = 0
        self.selected_backing = None
        self.selected_cache = None
        self._setup_ui()
        self._show_step(0)

    def _setup_ui(self):
        self.stack = Gtk.Stack()
        self.stack.set_transition_type(Gtk.StackTransitionType.SLIDE_LEFT_RIGHT)
        # Navigation buttons (bottom)
        nav_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        nav_box.set_margin_top(12)
        nav_box.set_margin_bottom(12)
        nav_box.set_margin_end(24)
        nav_box.set_halign(Gtk.Align.END)
        self.cancel_btn = Gtk.Button.new_with_label("Cancel")
        self.back_btn = Gtk.Button.new_with_label("Back")
        self.next_btn = Gtk.Button.new_with_label("Next")
        nav_box.append(self.cancel_btn)
        nav_box.append(self.back_btn)
        nav_box.append(self.next_btn)
        # Use a vertical box for main layout
        main_vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        main_vbox.append(self.stack)
        main_vbox.append(nav_box)
        self.set_content(main_vbox)

        # Step 0: Introduction
        intro_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        intro_box.set_margin_top(24)
        intro_box.set_margin_bottom(24)
        intro_box.set_margin_start(24)
        intro_box.set_margin_end(24)
        title = Gtk.Label()
        title.set_markup("<span size='xx-large' weight='bold'>Welcome to the Bcache Setup Wizard</span>")
        title.set_halign(Gtk.Align.START)
        intro_box.append(title)
        desc = Gtk.Label(label="Bcache lets you use a fast SSD or NVMe drive as a cache for a larger, slower HDD. This wizard will guide you through selecting drives and setting up Bcache step by step.\n\nYou will be able to review your choices before any changes are made.\n\n\u2022 The backing device is usually a large HDD.\n\u2022 The cache device is usually a fast SSD or NVMe.\n\u2022 All data on selected drives will be erased.")
        desc.set_wrap(True)
        desc.set_halign(Gtk.Align.START)
        intro_box.append(desc)
        self.intro_box = intro_box
        self.stack.add_titled(intro_box, "intro", "Introduction")

        # Step 1: Select Backing Device
        self.backing_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        self.backing_box.set_margin_top(24)
        self.backing_box.set_margin_bottom(24)
        self.backing_box.set_margin_start(24)
        self.backing_box.set_margin_end(24)
        backing_title = Gtk.Label()
        backing_title.set_markup("<span size='x-large' weight='bold'>Select Backing Device (HDD)</span>")
        backing_title.set_halign(Gtk.Align.START)
        self.backing_box.append(backing_title)
        backing_desc = Gtk.Label(label="The backing device is the main storage (usually a large HDD) that will be accelerated by the cache device. All data on this drive will be erased.")
        backing_desc.set_wrap(True)
        backing_desc.set_halign(Gtk.Align.START)
        self.backing_box.append(backing_desc)
        self.backing_list = Gtk.ListBox()
        self.backing_list.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.backing_box.append(self.backing_list)
        self.stack.add_titled(self.backing_box, "backing", "Backing Device")

        # Step 2: Select Cache Device
        self.cache_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        self.cache_box.set_margin_top(24)
        self.cache_box.set_margin_bottom(24)
        self.cache_box.set_margin_start(24)
        self.cache_box.set_margin_end(24)
        cache_title = Gtk.Label()
        cache_title.set_markup("<span size='x-large' weight='bold'>Select Cache Device (SSD/NVMe)</span>")
        cache_title.set_halign(Gtk.Align.START)
        self.cache_box.append(cache_title)
        cache_desc = Gtk.Label(label="The cache device is a fast SSD or NVMe drive that will be used to accelerate the backing device. You can skip this step to create a backing-only bcache device. All data on this drive will be erased.")
        cache_desc.set_wrap(True)
        cache_desc.set_halign(Gtk.Align.START)
        self.cache_list = Gtk.ListBox()
        self.cache_list.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.cache_box.append(self.cache_list)
        skip_btn = Gtk.Button.new_with_label("Skip (no cache device)")
        skip_btn.connect("clicked", self._on_skip_cache)
        self.cache_box.append(skip_btn)
        self.stack.add_titled(self.cache_box, "cache", "Cache Device")

        # Step 3: Cleanse Devices (NEW)
        self.cleanse_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        self.cleanse_box.set_margin_top(24)
        self.cleanse_box.set_margin_bottom(24)
        self.cleanse_box.set_margin_start(24)
        self.cleanse_box.set_margin_end(24)
        cleanse_title = Gtk.Label()
        cleanse_title.set_markup("<span size='x-large' weight='bold'>Cleanse Devices</span>")
        cleanse_title.set_halign(Gtk.Align.START)
        self.cleanse_box.append(cleanse_title)
        cleanse_warn = Gtk.Label(label="This will erase all data and signatures on the selected drives. This is required for Bcache to work. You must wipe both the backing and cache device (if selected).\n\nA terminal will open for each wipe, and you will be prompted for your sudo password.")
        cleanse_warn.set_wrap(True)
        cleanse_warn.set_halign(Gtk.Align.START)
        self.cleanse_box.append(cleanse_warn)
        # Wipe buttons (added dynamically)
        self.wipe_backing_btn = Gtk.Button.new_with_label("Wipe Backing Device")
        self.wipe_backing_btn.set_halign(Gtk.Align.START)
        self.wipe_backing_btn.connect("clicked", self._on_wipe_backing)
        self.cleanse_box.append(self.wipe_backing_btn)
        self.wipe_cache_btn = Gtk.Button.new_with_label("Wipe Cache Device")
        self.wipe_cache_btn.set_halign(Gtk.Align.START)
        self.wipe_cache_btn.connect("clicked", self._on_wipe_cache)
        self.cleanse_box.append(self.wipe_cache_btn)
        # Status labels
        self.wipe_backing_status = Gtk.Label(label="Not wiped yet.")
        self.wipe_backing_status.set_halign(Gtk.Align.START)
        self.cleanse_box.append(self.wipe_backing_status)
        self.wipe_cache_status = Gtk.Label(label="Not wiped yet.")
        self.wipe_cache_status.set_halign(Gtk.Align.START)
        self.cleanse_box.append(self.wipe_cache_status)
        self.stack.add_titled(self.cleanse_box, "cleanse", "Cleanse Devices")

        # Step 4: Review and Confirm
        self.review_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        self.review_box.set_margin_top(24)
        self.review_box.set_margin_bottom(24)
        self.review_box.set_margin_start(24)
        self.review_box.set_margin_end(24)
        review_title = Gtk.Label()
        review_title.set_markup("<span size='x-large' weight='bold'>Review and Confirm</span>")
        review_title.set_halign(Gtk.Align.START)
        self.review_box.append(review_title)
        self.review_summary = Gtk.Label()
        self.review_summary.set_wrap(True)
        self.review_summary.set_halign(Gtk.Align.START)
        self.review_box.append(self.review_summary)
        warning = Gtk.Label()
        warning.set_markup("<span foreground='red' weight='bold'>Warning: This operation will erase all data on the selected drives!</span>")
        warning.set_halign(Gtk.Align.START)
        self.review_box.append(warning)
        self.stack.add_titled(self.review_box, "review", "Review")

        # Step 5: Progress/Result
        self.result_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        self.result_box.set_margin_top(24)
        self.result_box.set_margin_bottom(24)
        self.result_box.set_margin_start(24)
        self.result_box.set_margin_end(24)
        self.result_label = Gtk.Label()
        self.result_label.set_wrap(True)
        self.result_label.set_halign(Gtk.Align.START)
        self.result_box.append(self.result_label)
        self.finish_btn = Gtk.Button.new_with_label("Finish")
        self.finish_btn.connect("clicked", lambda x: self.close())
        self.result_box.append(self.finish_btn)
        self.stack.add_titled(self.result_box, "result", "Result")
        self.finish_btn.set_sensitive(False)

        self.cancel_btn.connect("clicked", lambda x: self.close())
        self.back_btn.connect("clicked", self._on_back)
        self.next_btn.connect("clicked", self._on_next)

        # Step 2.5: Detach from Bcache (NEW)
        self.detach_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        self.detach_box.set_margin_top(24)
        self.detach_box.set_margin_bottom(24)
        self.detach_box.set_margin_start(24)
        self.detach_box.set_margin_end(24)
        detach_title = Gtk.Label()
        detach_title.set_markup("<span size='x-large' weight='bold'>Detach from Bcache</span>")
        detach_title.set_halign(Gtk.Align.START)
        self.detach_box.append(detach_title)
        self.detach_warn = Gtk.Label()
        self.detach_warn.set_wrap(True)
        self.detach_warn.set_halign(Gtk.Align.START)
        self.detach_box.append(self.detach_warn)
        # Detach buttons (added dynamically)
        self.detach_backing_btn = Gtk.Button.new_with_label("Detach Backing Device from Bcache")
        self.detach_backing_btn.set_halign(Gtk.Align.START)
        self.detach_backing_btn.connect("clicked", self._on_detach_backing)
        self.detach_box.append(self.detach_backing_btn)
        self.detach_cache_btn = Gtk.Button.new_with_label("Detach Cache Device from Bcache")
        self.detach_cache_btn.set_halign(Gtk.Align.START)
        self.detach_cache_btn.connect("clicked", self._on_detach_cache)
        self.detach_box.append(self.detach_cache_btn)
        # Status labels
        self.detach_backing_status = Gtk.Label(label="Not detached yet.")
        self.detach_backing_status.set_halign(Gtk.Align.START)
        self.detach_box.append(self.detach_backing_status)
        self.detach_cache_status = Gtk.Label(label="Not detached yet.")
        self.detach_cache_status.set_halign(Gtk.Align.START)
        self.detach_box.append(self.detach_cache_status)
        self.stack.add_titled(self.detach_box, "detach", "Detach from Bcache")

    def _show_step(self, step):
        self.current_step = step
        if step == 0:
            self.stack.set_visible_child(self.intro_box)
            self.back_btn.set_sensitive(False)
            self.next_btn.set_sensitive(True)
            self.next_btn.set_label("Next")
        elif step == 1:
            self._populate_backing_list()
            self.stack.set_visible_child(self.backing_box)
            self.back_btn.set_sensitive(True)
            self.next_btn.set_sensitive(True)
            self.next_btn.set_label("Next")
        elif step == 2:
            self._populate_cache_list()
            self.stack.set_visible_child(self.cache_box)
            self.back_btn.set_sensitive(True)
            self.next_btn.set_sensitive(True)
            self.next_btn.set_label("Next")
        elif step == 3:
            # Detach from Bcache step
            self._update_detach_ui()
            self.stack.set_visible_child(self.detach_box)
            self.back_btn.set_sensitive(True)
            can_next = (not self._backing_needs_detach or self._backing_detached) and (not self._cache_needs_detach or self._cache_detached)
            self.next_btn.set_sensitive(can_next)
            self.next_btn.set_label("Next")
        elif step == 4:
            # Cleanse Devices step
            self._update_cleanse_ui()
            self.stack.set_visible_child(self.cleanse_box)
            self.back_btn.set_sensitive(True)
            can_next = self._backing_wiped and (self.selected_cache is None or self._cache_wiped)
            self.next_btn.set_sensitive(can_next)
            self.next_btn.set_label("Next")
        elif step == 5:
            self._populate_review()
            self.stack.set_visible_child(self.review_box)
            self.back_btn.set_sensitive(True)
            self.next_btn.set_sensitive(True)
            self.next_btn.set_label("Confirm")
        elif step == 6:
            self.stack.set_visible_child(self.result_box)
            self.back_btn.set_sensitive(False)
            self.next_btn.set_sensitive(False)
            self.finish_btn.set_sensitive(False)

    def _on_next(self, btn):
        if self.current_step == 0:
            self._show_step(1)
        elif self.current_step == 1:
            selected = self.backing_list.get_selected_row()
            if not selected:
                self._show_error("Please select a backing device (HDD) to continue.")
                return
            self.selected_backing = selected.device
            self._show_step(2)
        elif self.current_step == 2:
            selected = self.cache_list.get_selected_row()
            if selected:
                self.selected_cache = selected.device
            self._show_step(3)
        elif self.current_step == 3:
            # Only allow if all needed detaches are done
            if (self._backing_needs_detach and not self._backing_detached) or (self._cache_needs_detach and not self._cache_detached):
                self._show_error("You must detach all devices from bcache before proceeding.")
                return
            self._show_step(4)
        elif self.current_step == 4:
            if not self._backing_wiped or (self.selected_cache and not self._cache_wiped):
                self._show_error("You must wipe both devices before proceeding.")
                return
            self._show_step(5)
        elif self.current_step == 5:
            self._show_step(6)
            self._run_bcache_creation()

    def _on_back(self, btn):
        if self.current_step == 1:
            self._show_step(0)
        elif self.current_step == 2:
            self._show_step(1)
        elif self.current_step == 3:
            self._show_step(2)
        elif self.current_step == 4:
            self._show_step(3)
        elif self.current_step == 5:
            self._show_step(4)

    def _on_skip_cache(self, btn):
        self.selected_cache = None
        self._show_step(3)

    def _populate_backing_list(self):
        # Clear previous
        while child := self.backing_list.get_first_child():
            self.backing_list.remove(child)
        # Add RAID arrays first
        if hasattr(RaidManager, 'get_raid_arrays'):
            for arr in RaidManager.get_raid_arrays():
                row = Gtk.ListBoxRow()
                box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
                label = Gtk.Label(label=f"{arr['name']} (RAID {arr['level']})")
                label.set_xalign(0)
                box.append(label)
                row.set_child(box)
                row.device = arr['name']
                self.backing_list.append(row)
        # Get all drives
        drives = DriveInfo.get_drives()
        # Filter for non-USB, non-NVMe, rotational (HDD)
        hdds = [d for d in drives if not d['is_usb'] and not d['is_nvme'] and d['rotational']]
        for drive in hdds:
            percent = DriveInfo.get_percent_used(drive['name'])
            percent_str = f" • {percent}% used" if percent is not None else " • Unknown usage"
            row = Gtk.ListBoxRow()
            box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
            label = Gtk.Label(label=f"{drive['model']} ({drive['name']}) - {drive['size']}{percent_str}")
            label.set_xalign(0)
            box.append(label)
            row.set_child(box)
            row.device = drive['name']
            self.backing_list.append(row)

    def _populate_cache_list(self):
        # Clear previous
        while child := self.cache_list.get_first_child():
            self.cache_list.remove(child)
        # Get all drives
        drives = DriveInfo.get_drives()
        # Filter for non-USB, non-rotational (SSD/NVMe)
        ssds = [d for d in drives if not d['is_usb'] and not d['rotational']]
        for drive in ssds:
            percent = DriveInfo.get_percent_used(drive['name'])
            percent_str = f" • {percent}% used" if percent is not None else " • Unknown usage"
            row = Gtk.ListBoxRow()
            box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
            label = Gtk.Label(label=f"{drive['model']} ({drive['name']}) - {drive['size']}{percent_str}")
            label.set_xalign(0)
            box.append(label)
            row.set_child(box)
            row.device = drive['name']
            self.cache_list.append(row)

    def _populate_review(self):
        backing = self.selected_backing or "(none)"
        cache = self.selected_cache or "(none)"
        summary = f"Backing device: {backing}\nCache device: {cache}\n\nAre you sure you want to proceed? This will erase all data on the selected drives."
        self.review_summary.set_text(summary)

    def _run_bcache_creation(self):
        self.result_label.set_text("Creating Bcache device... Please wait.")
        self.finish_btn.set_sensitive(False)
        def worker():
            import shlex, subprocess
            term = DriveInfo.detect_terminal()
            if not term:
                GLib.idle_add(self._show_bcache_result, False, "No supported terminal emulator found. Please install gnome-terminal, xterm, or similar.")
                return
            cmd = BcacheManager.build_bcache_command(self.selected_backing, self.selected_cache)
            shell_cmd = " ".join([shlex.quote(x) for x in cmd]) + "; read -n 1 -s -r -p 'Press any key to close...'"
            if "gnome-terminal" in term or "xfce4-terminal" in term or "lxterminal" in term:
                term_cmd = [term, "--", "bash", "-c", shell_cmd]
            elif "konsole" in term:
                term_cmd = [term, "-e", "bash", "-c", shell_cmd]
            else:
                term_cmd = [term, "-e", "bash", "-c", shell_cmd]
            try:
                subprocess.run(term_cmd, check=True)
                # After terminal closes, check if bcache device exists
                import time
                time.sleep(2)
                bcache_devices = BcacheManager.get_bcache_devices()
                if bcache_devices:
                    msg = f"Created bcache device(s): {[d['name'] for d in bcache_devices]}"
                    GLib.idle_add(self._show_bcache_result, True, msg)
                else:
                    GLib.idle_add(self._show_bcache_result, False, "No bcache device found after creation.")
            except Exception as e:
                GLib.idle_add(self._show_bcache_result, False, f"Failed to launch terminal: {e}")
        threading.Thread(target=worker, daemon=True).start()

    def _show_bcache_result(self, success, msg):
        if success:
            self.result_label.set_markup(f"<span foreground='green' weight='bold'>Bcache device created successfully!</span>\n\n{msg}")
        else:
            self.result_label.set_markup(f"<span foreground='red' weight='bold'>Failed to create Bcache device.</span>\n\n{msg}")
        self.finish_btn.set_sensitive(True)

    def _show_error(self, message):
        dialog = Adw.MessageDialog(transient_for=self, modal=True)
        dialog.set_heading("Error")
        dialog.set_body(message)
        dialog.add_response("ok", "OK")
        dialog.set_default_response("ok")
        dialog.connect("response", lambda d, r: d.close())
        dialog.present()

    def _on_wipe_backing(self, btn):
        device = self.selected_backing
        self._launch_wipefs(device, is_cache=False)

    def _on_wipe_cache(self, btn):
        device = self.selected_cache
        self._launch_wipefs(device, is_cache=True)

    def _launch_wipefs(self, device, is_cache):
        import shlex, subprocess
        term = DriveInfo.detect_terminal()
        if not term:
            self._show_error("No supported terminal emulator found. Please install gnome-terminal, xterm, or similar.")
            return
        wipe_cmd = f"sudo wipefs -a {shlex.quote(device)}; read -n 1 -s -r -p 'Press any key to close...'"
        if "gnome-terminal" in term or "xfce4-terminal" in term or "lxterminal" in term:
            cmd = [term, "--", "bash", "-c", wipe_cmd]
        elif "konsole" in term:
            cmd = [term, "-e", "bash", "-c", wipe_cmd]
        else:
            cmd = [term, "-e", "bash", "-c", wipe_cmd]
        try:
            subprocess.run(cmd, check=True)
            if is_cache:
                self._cache_wiped = True
                self.wipe_cache_status.set_text("Cache device wiped.")
            else:
                self._backing_wiped = True
                self.wipe_backing_status.set_text("Backing device wiped.")
            # Enable Next if both wiped
            can_next = self._backing_wiped and (self.selected_cache is None or self._cache_wiped)
            self.next_btn.set_sensitive(can_next)
        except Exception as e:
            if is_cache:
                self.wipe_cache_status.set_text(f"Failed: {e}")
            else:
                self.wipe_backing_status.set_text(f"Failed: {e}")

    def _update_cleanse_ui(self):
        # Hide cache wipe if no cache selected
        self._backing_wiped = False
        self._cache_wiped = False
        self.wipe_backing_status.set_text("Not wiped yet.")
        self.wipe_cache_status.set_text("Not wiped yet.")
        self.wipe_backing_btn.set_sensitive(True)
        if self.selected_cache:
            self.wipe_cache_btn.set_visible(True)
            self.wipe_cache_status.set_visible(True)
            self.wipe_cache_btn.set_sensitive(True)
        else:
            self.wipe_cache_btn.set_visible(False)
            self.wipe_cache_status.set_visible(False)

    def _update_detach_ui(self):
        import os, glob
        self._backing_needs_detach = False
        self._cache_needs_detach = False
        self._backing_detached = False
        self._cache_detached = False
        # Check if backing device is part of bcache
        if self.selected_backing:
            devname = os.path.basename(self.selected_backing)
            bcache_paths = glob.glob(f"/sys/block/bcache*/slaves/{devname}")
            if bcache_paths:
                self._backing_needs_detach = True
                self.detach_backing_btn.set_visible(True)
                self.detach_backing_status.set_visible(True)
                self.detach_backing_status.set_text("Not detached yet.")
            else:
                self.detach_backing_btn.set_visible(False)
                self.detach_backing_status.set_visible(False)
                self._backing_detached = True
        # Check if cache device is part of bcache
        if self.selected_cache:
            devname = os.path.basename(self.selected_cache)
            bcache_paths = glob.glob(f"/sys/block/bcache*/slaves/{devname}")
            if bcache_paths:
                self._cache_needs_detach = True
                self.detach_cache_btn.set_visible(True)
                self.detach_cache_status.set_visible(True)
                self.detach_cache_status.set_text("Not detached yet.")
            else:
                self.detach_cache_btn.set_visible(False)
                self.detach_cache_status.set_visible(False)
                self._cache_detached = True
        # Set warning text
        warn = []
        if self._backing_needs_detach:
            warn.append(f"Backing device {self.selected_backing} is currently attached to a bcache set. You must detach it before proceeding.")
        if self._cache_needs_detach:
            warn.append(f"Cache device {self.selected_cache} is currently attached to a bcache set. You must detach it before proceeding.")
        if not warn:
            warn.append("No devices need detaching. You can proceed.")
        self.detach_warn.set_text("\n".join(warn))
        # Hide cache detach if no cache selected
        if not self.selected_cache:
            self.detach_cache_btn.set_visible(False)
            self.detach_cache_status.set_visible(False)

    def _on_detach_backing(self, btn):
        device = self.selected_backing
        self._launch_detach(device, is_cache=False)

    def _on_detach_cache(self, btn):
        device = self.selected_cache
        self._launch_detach(device, is_cache=True)

    def _launch_detach(self, device, is_cache):
        import os, shlex, subprocess, glob
        term = DriveInfo.detect_terminal()
        if not term:
            self._show_error("No supported terminal emulator found. Please install gnome-terminal, xterm, or similar.")
            return
        # Find bcache sysfs path for this device
        devname = os.path.basename(device)
        bcache_paths = glob.glob(f"/sys/block/bcache*/slaves/{devname}")
        if not bcache_paths:
            if is_cache:
                self._cache_detached = True
                self.detach_cache_status.set_text("Cache device already detached.")
            else:
                self._backing_detached = True
                self.detach_backing_status.set_text("Backing device already detached.")
            return
        # Get bcacheX from path
        bcache_block = bcache_paths[0].split("/")[3]
        detach_cmd = f"echo 1 | sudo tee /sys/block/{bcache_block}/bcache/detach; read -n 1 -s -r -p 'Press any key to close...'"
        if "gnome-terminal" in term or "xfce4-terminal" in term or "lxterminal" in term:
            cmd = [term, "--", "bash", "-c", detach_cmd]
        elif "konsole" in term:
            cmd = [term, "-e", "bash", "-c", detach_cmd]
        else:
            cmd = [term, "-e", "bash", "-c", detach_cmd]
        try:
            subprocess.run(cmd, check=True)
            if is_cache:
                self._cache_detached = True
                self.detach_cache_status.set_text("Cache device detached.")
            else:
                self._backing_detached = True
                self.detach_backing_status.set_text("Backing device detached.")
            can_next = (not self._backing_needs_detach or self._backing_detached) and (not self._cache_needs_detach or self._cache_detached)
            self.next_btn.set_sensitive(can_next)
        except Exception as e:
            if is_cache:
                self.detach_cache_status.set_text(f"Failed: {e}")
            else:
                self.detach_backing_status.set_text(f"Failed: {e}")


class BcacheWindow(Adw.Window):
    """Bcache management window"""
    
    def __init__(self, parent):
        super().__init__()
        self.set_transient_for(parent)
        self.set_modal(True)
        self.set_default_size(800, 600)
        self.set_title("Bcache Management")
        
        self.setup_ui()
        self.refresh_bcache()
    
    def setup_ui(self):
        # Main layout
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.set_content(box)
        
        # Header
        header = Adw.HeaderBar()
        box.append(header)
        
        # Refresh button
        refresh_btn = Gtk.Button.new_from_icon_name("view-refresh-symbolic")
        refresh_btn.connect("clicked", lambda x: self.refresh_bcache())
        header.pack_start(refresh_btn)
        
        # Create new bcache button
        create_btn = Gtk.Button.new_with_label("Create Bcache")
        create_btn.add_css_class("suggested-action")
        create_btn.connect("clicked", self._show_create_dialog)
        header.pack_end(create_btn)
        
        # Content
        self.content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self.content.set_margin_top(12)
        self.content.set_margin_bottom(12)
        self.content.set_margin_start(12)
        self.content.set_margin_end(12)
        
        scrolled = Gtk.ScrolledWindow()
        scrolled.set_vexpand(True)
        scrolled.set_child(self.content)
        box.append(scrolled)
    
    def refresh_bcache(self):
        # Clear content
        while child := self.content.get_first_child():
            self.content.remove(child)
        
        # Check availability
        if not BcacheManager.check_bcache_available():
            error_group = Adw.PreferencesGroup()
            error_group.set_title("Bcache Not Available")
            error_group.set_description("Install bcache-tools: sudo apt install bcache-tools")
            self.content.append(error_group)
            return
        
        # Get bcache devices
        devices = BcacheManager.get_bcache_devices()
        
        # Show bcache devices if any
        if devices:
            devices_group = Adw.PreferencesGroup()
            devices_group.set_title("Bcache Devices")
            for device in devices:
                row = Adw.ActionRow()
                row.set_title(device['name'])
                row.set_subtitle(f"Size: {device['size']} • Mount: {device['mountpoint']}")
                icon = Gtk.Image.new_from_icon_name("drive-harddisk-symbolic")
                row.add_prefix(icon)
                devices_group.add(row)
            self.content.append(devices_group)
        
        # Always show available drives (HDDs first, then SSDs/NVMe)
        drives = DriveInfo.get_drives()
        hdds = [d for d in drives if not d['is_usb'] and not d['is_nvme'] and d['rotational']]
        ssds = [d for d in drives if not d['is_usb'] and not d['rotational']]
        drives_group = Adw.PreferencesGroup()
        drives_group.set_title("Available Drives (for Bcache)")
        if not hdds and not ssds:
            empty_row = Adw.ActionRow()
            empty_row.set_title("No available drives found")
            drives_group.add(empty_row)
        else:
            for drive in hdds + ssds:
                row = Adw.ActionRow()
                row.set_title(f"{drive['model']} ({drive['name']})")
                row.set_subtitle(f"{drive['size']} • {'HDD' if drive['rotational'] else 'SSD/NVMe'} • {drive['transport'].upper() if drive['transport'] else 'Unknown'}")
                icon_name = "drive-harddisk-symbolic" if drive['rotational'] else "drive-harddisk-solidstate-symbolic"
                icon = Gtk.Image.new_from_icon_name(icon_name)
                row.add_prefix(icon)
                drives_group.add(row)
        self.content.append(drives_group)
    
    def _show_create_dialog(self, button):
        # Launch the Bcache wizard
        wizard = BcacheWizard(self)
        wizard.present()


class RaidWizard(Adw.Window):
    """Step-by-step RAID 0/1 setup wizard"""
    def __init__(self, parent, refresh_callback=None):
        super().__init__()
        self.set_transient_for(parent)
        self.set_modal(True)
        self.set_default_size(600, 400)
        self.set_title("RAID Setup Wizard")
        self.current_step = 0
        self.selected_level = None
        self.selected_drives = []
        self.refresh_callback = refresh_callback
        self._setup_ui()
        self._show_step(0)

    def _setup_ui(self):
        self.stack = Gtk.Stack()
        self.stack.set_transition_type(Gtk.StackTransitionType.SLIDE_LEFT_RIGHT)
        nav_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        nav_box.set_margin_top(12)
        nav_box.set_margin_bottom(12)
        nav_box.set_margin_end(24)
        nav_box.set_halign(Gtk.Align.END)
        self.cancel_btn = Gtk.Button.new_with_label("Cancel")
        self.back_btn = Gtk.Button.new_with_label("Back")
        self.next_btn = Gtk.Button.new_with_label("Next")
        nav_box.append(self.cancel_btn)
        nav_box.append(self.back_btn)
        nav_box.append(self.next_btn)
        main_vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        main_vbox.append(self.stack)
        main_vbox.append(nav_box)
        self.set_content(main_vbox)

        # Step 0: Introduction
        intro_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        intro_box.set_margin_top(24)
        intro_box.set_margin_bottom(24)
        intro_box.set_margin_start(24)
        intro_box.set_margin_end(24)
        title = Gtk.Label()
        title.set_markup("<span size='xx-large' weight='bold'>Welcome to the RAID Setup Wizard</span>")
        title.set_halign(Gtk.Align.START)
        intro_box.append(title)
        desc = Gtk.Label(label=(
            "This wizard will help you set up a RAID 0 (Stripe) or RAID 1 (Mirror) array using two drives.\n\n"
            "<b>RAID 0 (Stripe):</b> Combines two drives for speed and capacity. <b>No redundancy</b>. If either drive fails, all data is lost.\n"
            "<b>Use for:</b> Maximum speed and space, non-critical data.\n\n"
            "<b>RAID 1 (Mirror):</b> Mirrors data across two drives. <b>Redundant</b>. If one drive fails, your data is safe.\n"
            "<b>Use for:</b> Important data, reliability over speed.\n\n"
            "<b>Warning:</b> Setting up RAID will erase all data on the selected drives!"
        ))
        desc.set_wrap(True)
        desc.set_halign(Gtk.Align.START)
        desc.set_use_markup(True)
        intro_box.append(desc)
        self.intro_box = intro_box
        self.stack.add_titled(intro_box, "intro", "Introduction")

        # Step 1: RAID Level Selection
        level_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        level_box.set_margin_top(24)
        level_box.set_margin_bottom(24)
        level_box.set_margin_start(24)
        level_box.set_margin_end(24)
        level_title = Gtk.Label()
        level_title.set_markup("<span size='x-large' weight='bold'>Select RAID Level</span>")
        level_title.set_halign(Gtk.Align.START)
        level_box.append(level_title)
        # GTK4: Use ListBox for radio-like selection
        self.level_list = Gtk.ListBox()
        self.level_list.set_selection_mode(Gtk.SelectionMode.SINGLE)
        for label in ["RAID 0 (Stripe)", "RAID 1 (Mirror)"]:
            row = Gtk.ListBoxRow()
            box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
            lbl = Gtk.Label(label=label)
            lbl.set_xalign(0)
            box.append(lbl)
            row.set_child(box)
            self.level_list.append(row)
        level_box.append(self.level_list)
        level_desc = Gtk.Label(label="Choose RAID 0 for speed/capacity, RAID 1 for redundancy.")
        level_desc.set_wrap(True)
        level_desc.set_halign(Gtk.Align.START)
        level_box.append(level_desc)
        self.level_box = level_box
        self.stack.add_titled(level_box, "level", "RAID Level")

        # Step 2: Drive Selection
        drive_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        drive_box.set_margin_top(24)
        drive_box.set_margin_bottom(24)
        drive_box.set_margin_start(24)
        drive_box.set_margin_end(24)
        drive_title = Gtk.Label()
        drive_title.set_markup("<span size='x-large' weight='bold'>Select Two Drives</span>")
        drive_title.set_halign(Gtk.Align.START)
        drive_box.append(drive_title)
        drive_desc = Gtk.Label(label="Select exactly two eligible drives. All data will be erased!")
        drive_desc.set_wrap(True)
        drive_desc.set_halign(Gtk.Align.START)
        drive_box.append(drive_desc)
        self.drive_list = Gtk.ListBox()
        self.drive_list.set_selection_mode(Gtk.SelectionMode.MULTIPLE)
        drive_box.append(self.drive_list)
        self.drive_box = drive_box
        self.stack.add_titled(drive_box, "drives", "Drives")

        # Step 3: Cleanse Devices (NEW)
        self.cleanse_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        self.cleanse_box.set_margin_top(24)
        self.cleanse_box.set_margin_bottom(24)
        self.cleanse_box.set_margin_start(24)
        self.cleanse_box.set_margin_end(24)
        cleanse_title = Gtk.Label()
        cleanse_title.set_markup("<span size='x-large' weight='bold'>Cleanse Devices</span>")
        cleanse_title.set_halign(Gtk.Align.START)
        self.cleanse_box.append(cleanse_title)
        cleanse_warn = Gtk.Label(label="This will erase all data and signatures on the selected drives. This is required for RAID to work. You must wipe both drives.\n\nA terminal will open for each wipe, and you will be prompted for your sudo password.")
        cleanse_warn.set_wrap(True)
        cleanse_warn.set_halign(Gtk.Align.START)
        self.cleanse_box.append(cleanse_warn)
        # Wipe buttons (added dynamically)
        self.wipe_drive1_btn = Gtk.Button.new_with_label("Wipe Drive 1")
        self.wipe_drive1_btn.set_halign(Gtk.Align.START)
        self.wipe_drive1_btn.connect("clicked", self._on_wipe_drive1)
        self.cleanse_box.append(self.wipe_drive1_btn)
        self.wipe_drive2_btn = Gtk.Button.new_with_label("Wipe Drive 2")
        self.wipe_drive2_btn.set_halign(Gtk.Align.START)
        self.wipe_drive2_btn.connect("clicked", self._on_wipe_drive2)
        self.cleanse_box.append(self.wipe_drive2_btn)
        # Status labels
        self.wipe_drive1_status = Gtk.Label(label="Not wiped yet.")
        self.wipe_drive1_status.set_halign(Gtk.Align.START)
        self.cleanse_box.append(self.wipe_drive1_status)
        self.wipe_drive2_status = Gtk.Label(label="Not wiped yet.")
        self.wipe_drive2_status.set_halign(Gtk.Align.START)
        self.cleanse_box.append(self.wipe_drive2_status)
        self.stack.add_titled(self.cleanse_box, "cleanse", "Cleanse Devices")

        # Step 4: Review and Confirm
        self.review_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        self.review_box.set_margin_top(24)
        self.review_box.set_margin_bottom(24)
        self.review_box.set_margin_start(24)
        self.review_box.set_margin_end(24)
        review_title = Gtk.Label()
        review_title.set_markup("<span size='x-large' weight='bold'>Review and Confirm</span>")
        review_title.set_halign(Gtk.Align.START)
        self.review_box.append(review_title)
        self.review_summary = Gtk.Label()
        self.review_summary.set_wrap(True)
        self.review_summary.set_halign(Gtk.Align.START)
        self.review_box.append(self.review_summary)
        warning = Gtk.Label()
        warning.set_markup("<span foreground='red' weight='bold'>Warning: This operation will erase all data on the selected drives!</span>")
        warning.set_halign(Gtk.Align.START)
        self.review_box.append(warning)
        self.stack.add_titled(self.review_box, "review", "Review")

        # Step 5: Progress/Result
        self.result_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        self.result_box.set_margin_top(24)
        self.result_box.set_margin_bottom(24)
        self.result_box.set_margin_start(24)
        self.result_box.set_margin_end(24)
        self.result_label = Gtk.Label()
        self.result_label.set_wrap(True)
        self.result_label.set_halign(Gtk.Align.START)
        self.result_box.append(self.result_label)
        self.finish_btn = Gtk.Button.new_with_label("Finish")
        self.finish_btn.connect("clicked", lambda x: self.close())
        self.result_box.append(self.finish_btn)
        self.stack.add_titled(self.result_box, "result", "Result")
        self.finish_btn.set_sensitive(False)

        self.cancel_btn.connect("clicked", lambda x: self.close())
        self.back_btn.connect("clicked", self._on_back)
        self.next_btn.connect("clicked", self._on_next)

    def _show_step(self, step):
        self.current_step = step
        if step == 0:
            self.stack.set_visible_child(self.intro_box)
            self.back_btn.set_sensitive(False)
            self.next_btn.set_sensitive(True)
            self.next_btn.set_label("Next")
        elif step == 1:
            self.stack.set_visible_child(self.level_box)
            self.back_btn.set_sensitive(True)
            self.next_btn.set_sensitive(True)
            self.next_btn.set_label("Next")
        elif step == 2:
            self._populate_drive_list()
            self.stack.set_visible_child(self.drive_box)
            self.back_btn.set_sensitive(True)
            self.next_btn.set_sensitive(True)
            self.next_btn.set_label("Next")
        elif step == 3:
            # Cleanse Devices step
            self._update_cleanse_ui()
            self.stack.set_visible_child(self.cleanse_box)
            self.back_btn.set_sensitive(True)
            can_next = self._drive1_wiped and self._drive2_wiped
            self.next_btn.set_sensitive(can_next)
            self.next_btn.set_label("Next")
        elif step == 4:
            self._populate_review()
            self.stack.set_visible_child(self.review_box)
            self.back_btn.set_sensitive(True)
            self.next_btn.set_sensitive(True)
            self.next_btn.set_label("Confirm")
        elif step == 5:
            self.stack.set_visible_child(self.result_label.get_parent())
            self.back_btn.set_sensitive(False)
            self.next_btn.set_sensitive(False)
            self.finish_btn.set_sensitive(False)

    def _on_next(self, btn):
        if self.current_step == 0:
            self._show_step(1)
        elif self.current_step == 1:
            selected_row = self.level_list.get_selected_row()
            if not selected_row:
                self._show_error("Please select a RAID level to continue.")
                return
            if selected_row.get_index() == 0:
                self.selected_level = 0
            else:
                self.selected_level = 1
            self._show_step(2)
        elif self.current_step == 2:
            selected_rows = self.drive_list.get_selected_rows()
            if len(selected_rows) != 2:
                self._show_error("Please select exactly two drives.")
                return
            self.selected_drives = [row.device for row in selected_rows]
            self._show_step(3)
        elif self.current_step == 3:
            if not self._drive1_wiped or not self._drive2_wiped:
                self._show_error("You must wipe both drives before proceeding.")
                return
            self._show_step(4)
        elif self.current_step == 4:
            self._show_step(5)
            self._run_raid_creation()

    def _on_back(self, btn):
        if self.current_step == 1:
            self._show_step(0)
        elif self.current_step == 2:
            self._show_step(1)
        elif self.current_step == 3:
            self._show_step(2)

    def _on_wipe_drive1(self, btn):
        device = self.selected_drives[0]
        self._launch_wipefs(device, is_drive2=False)

    def _on_wipe_drive2(self, btn):
        device = self.selected_drives[1]
        self._launch_wipefs(device, is_drive2=True)

    def _launch_wipefs(self, device, is_drive2):
        import shlex, subprocess
        term = DriveInfo.detect_terminal()
        if not term:
            self._show_error("No supported terminal emulator found. Please install gnome-terminal, xterm, or similar.")
            return
        wipe_cmd = f"sudo wipefs -a {shlex.quote(device)}; read -n 1 -s -r -p 'Press any key to close...'"
        if "gnome-terminal" in term or "xfce4-terminal" in term or "lxterminal" in term:
            cmd = [term, "--", "bash", "-c", wipe_cmd]
        elif "konsole" in term:
            cmd = [term, "-e", "bash", "-c", wipe_cmd]
        else:
            cmd = [term, "-e", "bash", "-c", wipe_cmd]
        try:
            subprocess.run(cmd, check=True)
            if is_drive2:
                self._drive2_wiped = True
                self.wipe_drive2_status.set_text("Drive 2 wiped.")
            else:
                self._drive1_wiped = True
                self.wipe_drive1_status.set_text("Drive 1 wiped.")
            can_next = self._drive1_wiped and self._drive2_wiped
            self.next_btn.set_sensitive(can_next)
        except Exception as e:
            if is_drive2:
                self.wipe_drive2_status.set_text(f"Failed: {e}")
            else:
                self.wipe_drive1_status.set_text(f"Failed: {e}")

    def _update_cleanse_ui(self):
        self._drive1_wiped = False
        self._drive2_wiped = False
        self.wipe_drive1_status.set_text("Not wiped yet.")
        self.wipe_drive2_status.set_text("Not wiped yet.")
        self.wipe_drive1_btn.set_sensitive(True)
        self.wipe_drive2_btn.set_sensitive(True)

    def _populate_drive_list(self):
        while child := self.drive_list.get_first_child():
            self.drive_list.remove(child)
        # List all drives, RAID, and Bcache devices
        drives = DriveInfo.get_drives()
        # Add RAID arrays
        if hasattr(RaidManager, 'get_raid_arrays'):
            for arr in RaidManager.get_raid_arrays():
                row = Gtk.ListBoxRow()
                box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
                label = Gtk.Label(label=f"{arr['name']} (RAID {arr['level']})")
                label.set_xalign(0)
                box.append(label)
                row.set_child(box)
                row.device = arr['name']
                self.drive_list.append(row)
        # Add Bcache devices from lsblk
        bcache_names = set()
        if hasattr(BcacheManager, 'get_bcache_devices'):
            for bdev in BcacheManager.get_bcache_devices():
                devname = f"/dev/{bdev['name']}"
                bcache_names.add(devname)
                row = Gtk.ListBoxRow()
                box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
                label = Gtk.Label(label=f"{devname} (Bcache)")
                label.set_xalign(0)
                box.append(label)
                row.set_child(box)
                row.device = devname
                self.drive_list.append(row)
        # Add all /dev/bcache* devices that exist (avoid duplicates)
        import glob, os
        for path in glob.glob("/dev/bcache*"):
            if os.path.exists(path) and path not in bcache_names:
                row = Gtk.ListBoxRow()
                box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
                label = Gtk.Label(label=f"{path} (Bcache)")
                label.set_xalign(0)
                box.append(label)
                row.set_child(box)
                row.device = path
                self.drive_list.append(row)
        # Add regular drives
        for drive in drives:
            row = Gtk.ListBoxRow()
            box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
            label = Gtk.Label(label=f"{drive['model']} ({drive['name']}) - {drive['size']}")
            label.set_xalign(0)
            box.append(label)
            row.set_child(box)
            row.device = drive['name']
            self.drive_list.append(row)

    def _populate_review(self):
        level_str = "RAID 0 (Stripe)" if self.selected_level == 0 else "RAID 1 (Mirror)"
        drives_str = "\n".join(self.selected_drives)
        summary = f"RAID Level: {level_str}\nDrives:\n{drives_str}\n\nAre you sure you want to proceed? This will erase all data on the selected drives."
        self.review_summary.set_text(summary)

    def _run_raid_creation(self):
        self.result_label.set_text("Creating RAID array... Please wait.")
        self.finish_btn.set_sensitive(False)
        def worker():
            import shlex, subprocess
            arrays = RaidManager.get_raid_arrays()
            used_names = {a['name'] for a in arrays}
            for i in range(0, 10):
                array_name = f"md{i}"
                if f"/dev/{array_name}" not in used_names:
                    break
            else:
                array_name = "md10"
            # Launch mdadm in terminal
            term = DriveInfo.detect_terminal()
            if not term:
                GLib.idle_add(self._show_raid_result, False, "No supported terminal emulator found. Please install gnome-terminal, xterm, or similar.")
                return
            level = self.selected_level
            devices = self.selected_drives
            mdadm_cmd = f"sudo mdadm --create /dev/{array_name} --level={level} --raid-devices=2 {shlex.quote(devices[0])} {shlex.quote(devices[1])}; read -n 1 -s -r -p 'Press any key to close...'"
            if "gnome-terminal" in term or "xfce4-terminal" in term or "lxterminal" in term:
                cmd = [term, "--", "bash", "-c", mdadm_cmd]
            elif "konsole" in term:
                cmd = [term, "-e", "bash", "-c", mdadm_cmd]
            else:
                cmd = [term, "-e", "bash", "-c", mdadm_cmd]
            try:
                subprocess.run(cmd, check=True)
                GLib.idle_add(self._show_raid_result, True, f"RAID array /dev/{array_name} created (check terminal for details)")
            except Exception as e:
                GLib.idle_add(self._show_raid_result, False, f"Failed to create RAID array: {e}")
        threading.Thread(target=worker, daemon=True).start()

    def _show_raid_result(self, success, msg):
        if success:
            self.result_label.set_markup(f"<span foreground='green' weight='bold'>RAID array created successfully!</span>\n\n{msg}")
            if self.refresh_callback:
                self.refresh_callback()
        else:
            self.result_label.set_markup(f"<span foreground='red' weight='bold'>Failed to create RAID array.</span>\n\n{msg}")
        self.finish_btn.set_sensitive(True)

    def _show_error(self, message):
        dialog = Adw.MessageDialog(transient_for=self, modal=True)
        dialog.set_heading("Error")
        dialog.set_body(message)
        dialog.add_response("ok", "OK")
        dialog.set_default_response("ok")
        dialog.connect("response", lambda d, r: d.close())
        dialog.present()


class RaidWindow(Adw.Window):
    """RAID management window"""
    
    def __init__(self, parent):
        super().__init__()
        self.set_transient_for(parent)
        self.set_modal(True)
        self.set_default_size(800, 600)
        self.set_title("RAID Management")
        
        self.setup_ui()
        self.refresh_raid()
    
    def setup_ui(self):
        # Main layout
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.set_content(box)
        
        # Header
        header = Adw.HeaderBar()
        box.append(header)
        
        # Refresh button
        refresh_btn = Gtk.Button.new_from_icon_name("view-refresh-symbolic")
        refresh_btn.connect("clicked", lambda x: self.refresh_raid())
        header.pack_start(refresh_btn)
        
        # Create new RAID button
        create_btn = Gtk.Button.new_with_label("Create RAID")
        create_btn.add_css_class("suggested-action")
        create_btn.connect("clicked", self._show_create_dialog)
        header.pack_end(create_btn)
        
        # Content
        self.content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self.content.set_margin_top(12)
        self.content.set_margin_bottom(12)
        self.content.set_margin_start(12)
        self.content.set_margin_end(12)
        
        scrolled = Gtk.ScrolledWindow()
        scrolled.set_vexpand(True)
        scrolled.set_child(self.content)
        box.append(scrolled)
    
    def refresh_raid(self):
        # Clear content
        while child := self.content.get_first_child():
            self.content.remove(child)
        
        # Check availability
        if not RaidManager.check_mdadm_available():
            error_group = Adw.PreferencesGroup()
            error_group.set_title("RAID Not Available")
            error_group.set_description("Install mdadm: sudo apt install mdadm")
            self.content.append(error_group)
            return
        
        # Get RAID arrays
        arrays = RaidManager.get_raid_arrays()
        
        if not arrays:
            empty_group = Adw.PreferencesGroup()
            empty_group.set_title("No RAID Arrays")
            empty_group.set_description("Create a RAID array to get started")
            self.content.append(empty_group)
            return
        
        # Show arrays
        arrays_group = Adw.PreferencesGroup()
        arrays_group.set_title("RAID Arrays")
        
        for array in arrays:
            row = Adw.ActionRow()
            row.set_title(array['name'])
            row.set_subtitle(f"Level: {array['level']} • Status: {array['status']}")
            
            icon = Gtk.Image.new_from_icon_name("drive-multidisk-symbolic")
            row.add_prefix(icon)
            arrays_group.add(row)
        
        self.content.append(arrays_group)
    
    def _show_create_dialog(self, button):
        wizard = RaidWizard(self, refresh_callback=self.refresh_raid)
        wizard.present()


class DriveDetailWindow(Adw.Window):
    """Detailed drive information window"""
    
    def __init__(self, parent, drive_data):
        super().__init__()
        self.set_transient_for(parent)
        self.set_modal(True)
        self.set_default_size(600, 500)
        self.set_title(f"Drive Details - {drive_data['display_name']}")
        
        self.drive_data = drive_data
        self.setup_ui()
        self.load_details()
    
    def setup_ui(self):
        # Main layout with ToastOverlay for notifications
        self.toast_overlay = Adw.ToastOverlay()
        self.set_content(self.toast_overlay)
        
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.toast_overlay.set_child(box)
        
        # Header
        header = Adw.HeaderBar()
        box.append(header)
        
        # Refresh button
        refresh_btn = Gtk.Button.new_from_icon_name("view-refresh-symbolic")
        refresh_btn.connect("clicked", lambda x: self.load_details())
        header.pack_start(refresh_btn)
        
        # Scrolled content
        scrolled = Gtk.ScrolledWindow()
        scrolled.set_vexpand(True)
        box.append(scrolled)
        
        # Content box
        self.content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self.content.set_margin_top(12)
        self.content.set_margin_bottom(12)
        self.content.set_margin_start(12)
        self.content.set_margin_end(12)
        scrolled.set_child(self.content)
    
    def load_details(self):
        # Clear existing content
        while child := self.content.get_first_child():
            self.content.remove(child)
        
        # Show loading
        spinner = Gtk.Spinner()
        spinner.start()
        loading_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        loading_box.set_halign(Gtk.Align.CENTER)
        loading_box.append(spinner)
        loading_box.append(Gtk.Label(label="Loading drive information..."))
        self.content.append(loading_box)
        
        # Load data in thread
        threading.Thread(target=self._load_data_thread, daemon=True).start()
    
    def _load_data_thread(self):
        smart_info = DriveInfo.get_smart_info(self.drive_data['name'])
        GLib.idle_add(self._update_ui, smart_info)
    
    def _update_ui(self, smart_info):
        # Clear loading spinner
        while child := self.content.get_first_child():
            self.content.remove(child)
        
        # Basic info group
        basic_group = Adw.PreferencesGroup()
        basic_group.set_title("Basic Information")
        
        info_items = [
            ("Device", self.drive_data['name']),
            ("Model", self.drive_data['model']),
            ("Size", self.drive_data['size']),
            ("Transport", self.drive_data['transport']),
            ("Type", self._get_drive_type(self.drive_data)),
        ]
        
        # Add USB-specific info
        if self.drive_data.get('is_usb'):
            info_items.append(("Removable", "Yes" if self.drive_data.get('is_removable') else "No"))
            info_items.append(("Hotplug", "Yes" if self.drive_data.get('is_hotplug') else "No"))
        elif self.drive_data.get('is_removable') and self.drive_data['transport'] not in ['sata', 'ata']:
            info_items.append(("Removable", "Yes" if self.drive_data.get('is_removable') else "No"))
            info_items.append(("Hotplug", "Yes" if self.drive_data.get('is_hotplug') else "No"))
        
        for title, value in info_items:
            row = Adw.ActionRow()
            row.set_title(title)
            row.set_subtitle(str(value))
            if isinstance(row, Gtk.Box):
                print("[ERROR] Attempt to add GtkBox to basic_group!")
            basic_group.add(row)
        
        self.content.append(basic_group)

        # --- Technical Details ---
        tech_group = Adw.PreferencesGroup()
        tech_group.set_title("Technical Details")
        tech = DriveInfo.get_technical_details(self.drive_data['name'])
        if 'error' in tech:
            row = Adw.ActionRow()
            row.set_title("Error")
            row.set_subtitle(tech['error'])
            if isinstance(row, Gtk.Box):
                print("[ERROR] Attempt to add GtkBox to tech_group!")
            tech_group.add(row)
        else:
            row = Adw.ActionRow()
            row.set_title("Rotation/Speed/Gen")
            row.set_subtitle(str(tech.get('rotation_speed', 'Unknown')))
            if isinstance(row, Gtk.Box):
                print("[ERROR] Attempt to add GtkBox to tech_group!")
            tech_group.add(row)
            row = Adw.ActionRow()
            row.set_title("TRIM Support")
            row.set_subtitle(str(tech.get('trim')))
            if isinstance(row, Gtk.Box):
                print("[ERROR] Attempt to add GtkBox to tech_group!")
            tech_group.add(row)
            row = Adw.ActionRow()
            row.set_title("Link Speed")
            row.set_subtitle(str(tech.get('link_speed', 'Unknown')))
            if isinstance(row, Gtk.Box):
                print("[ERROR] Attempt to add GtkBox to tech_group!")
            tech_group.add(row)
            row = Adw.ActionRow()
            row.set_title("Partition Table")
            row.set_subtitle(str(tech.get('partition_table', 'Unknown')))
            if isinstance(row, Gtk.Box):
                print("[ERROR] Attempt to add GtkBox to tech_group!")
            tech_group.add(row)
            if tech.get('in_raid'):
                row = Adw.ActionRow()
                row.set_title("RAID Member")
                row.set_subtitle("This drive is part of a RAID array.")
                if isinstance(row, Gtk.Box):
                    print("[ERROR] Attempt to add GtkBox to tech_group!")
                tech_group.add(row)
            if tech.get('in_bcache'):
                row = Adw.ActionRow()
                row.set_title("Bcache Member")
                row.set_subtitle("This drive is used as a Bcache device.")
                if isinstance(row, Gtk.Box):
                    print("[ERROR] Attempt to add GtkBox to tech_group!")
                tech_group.add(row)
        self.content.append(tech_group)

        # --- Partition Info ---
        part_group = Adw.PreferencesGroup()
        part_group.set_title("Partitions")
        partitions = DriveInfo.get_partitions(self.drive_data['name'])
        if not partitions:
            row = Adw.ActionRow()
            row.set_title("No partitions found")
            if isinstance(row, Gtk.Box):
                print("[ERROR] Attempt to add GtkBox to part_group!")
            part_group.add(row)
        else:
            for part in partitions:
                row = Adw.ActionRow()
                row.set_title(f"/dev/{part['name']}")
                subtitle = f"{part['size']} • {part['fstype'] or 'Unknown FS'}"
                if part['mountpoint']:
                    subtitle += f" • Mounted at {part['mountpoint']}"
                if part['used']:
                    try:
                        percent_used = int(part['used'].replace('%',''))
                        percent_free = 100 - percent_used
                        subtitle += f" • {percent_used}% used, {percent_free}% free"
                    except Exception:
                        subtitle += f" • {part['used']} used"
                else:
                    subtitle += " • Unknown usage"
                if isinstance(row, Gtk.Box):
                    print("[ERROR] Attempt to add GtkBox to part_group!")
                row.set_subtitle(subtitle)
                part_group.add(row)
        self.content.append(part_group)

        # --- SMART/NVMe Attributes Table ---
        # Only show if SMART data is available and attributes are present
        if smart_info['available'] and smart_info.get('attributes'):
            attr_group = Adw.PreferencesGroup()
            attr_group.set_title("SMART/NVMe Attributes")
            # Summary health status
            summary_row = Adw.ActionRow()
            summary_row.set_title("Health Status")
            summary_row.set_subtitle("Healthy" if smart_info['health'] else "Warning/Failing")
            attr_group.add(summary_row)
            # Show total reads/writes if available
            reads = writes = None
            for attr in smart_info['attributes']:
                n = attr.get('name', '')
                v = attr.get('value', attr.get('raw', ''))
                if n in ('Total_LBAs_Written', 'Data_Units_Written'):
                    writes = v
                if n in ('Total_LBAs_Read', 'Data_Units_Read'):
                    reads = v
            if reads:
                row = Adw.ActionRow()
                row.set_title("Total Reads")
                row.set_subtitle(str(reads))
                attr_group.add(row)
            if writes:
                row = Adw.ActionRow()
                row.set_title("Total Writes")
                row.set_subtitle(str(writes))
                attr_group.add(row)
            # Tooltips for important attributes
            tooltips = {
                'Reallocated_Sector_Ct': 'Bad sectors reallocated by the drive. High values indicate failing sectors.',
                'Wear_Leveling_Count': 'SSD/NVMe wear level. Lower is worse.',
                'Media_Wearout_Indicator': 'SSD/NVMe wear indicator. Lower is worse.',
                'Temperature_Celsius': 'Drive temperature in Celsius.',
                'Power_On_Hours': 'Total hours powered on.',
                'Power_Cycle_Count': 'Number of power cycles.',
                'Total_LBAs_Written': 'Total data written.',
                'Data_Units_Written': 'NVMe: total data written.',
                'Percentage Used': 'NVMe: percent of device life used.'
            }
            # Highlight key attributes
            key_attrs = [
                'Reallocated_Sector_Ct', 'Wear_Leveling_Count', 'Media_Wearout_Indicator',
                'Temperature_Celsius', 'Power_On_Hours', 'Power_Cycle_Count',
                'Total_LBAs_Written', 'Data_Units_Written', 'Percentage Used'
            ]
            # Add each attribute as an ActionRow
            for attr in smart_info['attributes']:
                name = attr.get('name', attr.get('id', ''))
                value = attr.get('value', attr.get('raw', ''))
                color = None
                if name in key_attrs:
                    # Simple logic: green for good, yellow for warning, red for critical
                    if name in ('Reallocated_Sector_Ct', 'Media_Wearout_Indicator', 'Wear_Leveling_Count', 'Percentage Used'):
                        try:
                            v = int(value.split()[0].replace('%',''))
                            if v < 50:
                                color = '#7CFC8C'  # light green
                            elif v < 80:
                                color = '#FFD700'  # yellow
                            else:
                                color = '#FF6347'  # red
                        except Exception:
                            color = '#7CFC8C'
                    elif name == 'Temperature_Celsius':
                        try:
                            v = int(value.split()[0])
                            if v < 45:
                                color = '#7CFC8C'
                            elif v < 55:
                                color = '#FFD700'
                            else:
                                color = '#FF6347'
                        except Exception:
                            color = '#7CFC8C'
                    else:
                        color = '#7CFC8C'
                else:
                    color = '#7CFC8C'
                # Compose value markup
                if color:
                    value_markup = f'<span foreground="{color}"><b>{value}</b></span>'
                else:
                    value_markup = value
                row = Adw.ActionRow()
                row.set_title(name)
                row.set_subtitle("")
                value_label = Gtk.Label()
                value_label.set_markup(value_markup)
                value_label.set_xalign(1)
                row.add_suffix(value_label)
                # Tooltip
                if name in tooltips:
                    row.set_subtitle(tooltips[name])
                    value_label.set_tooltip_text(tooltips[name])
                attr_group.add(row)
            # Copy to clipboard button as an ActionRow
            copy_row = Adw.ActionRow()
            copy_btn = Gtk.Button.new_with_label("Copy Raw SMART Data")
            def on_copy(btn):
                clipboard = Gtk.Clipboard.get_default(Gtk.Display.get_default())
                clipboard.set_text(smart_info['output'], -1)
            copy_btn.connect("clicked", on_copy)
            copy_row.set_title("")
            copy_row.add_suffix(copy_btn)
            attr_group.add(copy_row)
            self.content.append(attr_group)
        else:
            # Minimal, clean: just a centered label and the button
            info_label = Gtk.Label()
            info_label.set_text("SMART data is only available in the terminal.")
            info_label.set_wrap(True)
            info_label.set_halign(Gtk.Align.CENTER)
            info_label.set_margin_top(12)


    def _run_test_and_refresh(self, test_type):
        device = self.drive_data['name']
        def run_and_refresh():
            success, message = DriveInfo.run_smart_test(device, test_type)
            import time
            time.sleep(2)
            GLib.idle_add(self.load_details)
            toast = Adw.Toast.new(f"{test_type.title()} test: {'Started' if success else 'Failed'} - {message}")
            toast.set_timeout(6)
            self.toast_overlay.add_toast(toast)
        import threading
        threading.Thread(target=run_and_refresh, daemon=True).start()

    def _get_drive_type(self, drive):
        if drive['is_nvme']:
            return "NVMe"
        elif drive['rotational']:
            return "HDD"
        else:
            return "SSD"


class SystemInfoWindow(Adw.Window):
    """System information window"""
    def __init__(self, parent):
        super().__init__()
        self.set_transient_for(parent)
        self.set_modal(False)
        self.set_default_size(600, 400)
        self.set_title("System Information")
        self._setup_ui()
        self._load_info()

    def _setup_ui(self):
        self.content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        self.content.set_margin_top(18)
        self.content.set_margin_bottom(18)
        self.content.set_margin_start(18)
        self.content.set_margin_end(18)
        # Add close button at the top
        close_btn = Gtk.Button.new_from_icon_name("window-close-symbolic")
        close_btn.set_halign(Gtk.Align.END)
        close_btn.connect("clicked", lambda x: self.close())
        self.content.append(close_btn)
        self.set_content(self.content)

    def _load_info(self):
        # OS info
        import subprocess
        try:
            os_info = subprocess.run(["lsb_release", "-d"], capture_output=True, text=True)
            os_str = os_info.stdout.strip().split(':', 1)[-1].strip() if os_info.returncode == 0 else platform.platform()
        except Exception:
            os_str = platform.platform()
        row = Adw.ActionRow()
        row.set_title("Operating System")
        row.set_subtitle(os_str)
        self.content.append(row)
        # CPU info
        try:
            with open("/proc/cpuinfo") as f:
                lines = f.readlines()
            model = next((l.split(":",1)[1].strip() for l in lines if l.lower().startswith("model name")), "Unknown")
        except Exception:
            model = "Unknown"
        row = Adw.ActionRow()
        row.set_title("CPU")
        row.set_subtitle(model)
        self.content.append(row)
        # RAM info
        try:
            with open("/proc/meminfo") as f:
                lines = f.readlines()
            memtotal_kb = int(next((l.split(":",1)[1].strip().split()[0] for l in lines if l.startswith("MemTotal")), "0"))
            memtotal_gb = memtotal_kb / 1024 / 1024
            ram_str = f"{memtotal_gb:.2f} GB"
        except Exception:
            ram_str = "Unknown"
        row = Adw.ActionRow()
        row.set_title("RAM")
        row.set_subtitle(ram_str)
        self.content.append(row)
        # GPU info
        try:
            gpu_info = subprocess.run(["lspci"], capture_output=True, text=True)
            gpus = [l for l in gpu_info.stdout.splitlines() if "VGA compatible controller" in l or "3D controller" in l]
            gpu_str = gpus[0] if gpus else "Unknown"
        except Exception:
            gpu_str = "Unknown"
        row = Adw.ActionRow()
        row.set_title("GPU")
        row.set_subtitle(gpu_str)
        self.content.append(row)
        # PSU info (not available programmatically)
        row = Adw.ActionRow()
        row.set_title("PSU")
        row.set_subtitle("Not available (desktop Linux does not report PSU info)")
        self.content.append(row)


class MainWindow(Adw.ApplicationWindow):
    """Main application window"""
    
    def __init__(self, app):
        super().__init__(application=app)
        self.set_title("DarkDiskz - Drive Monitor")
        self.set_default_size(800, 600)
        self.main_drives = []
        self.usb_drives = []
        self.show_hamster = True  # Setting for hamster icon
        self.setup_ui()
        self.refresh_drives()
        # --- Real-time USB detection using pyudev (GLib integration) ---
        if pyudev is not None:
            self._start_udev_monitor()
        else:
            print("pyudev not installed: USB hotplug detection disabled.")
    
    def setup_ui(self):
        # Main layout
        main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.set_content(main_box)
        
        # Header bar
        header = Adw.HeaderBar()
        main_box.append(header)
        
        # Refresh button
        refresh_btn = Gtk.Button.new_from_icon_name("view-refresh-symbolic")
        refresh_btn.set_tooltip_text("Refresh drive list")
        refresh_btn.connect("clicked", lambda x: self.refresh_drives())
        header.pack_start(refresh_btn)
        
        # Title
        title = Adw.WindowTitle()
        title.set_title("DarkDiskz")
        title.set_subtitle("Advanced Drive Analysis")
        header.set_title_widget(title)
        
        # Content area with sidebar
        content_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        main_box.append(content_box)
        
        # Left sidebar - now with expandable menu structure
        sidebar = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        sidebar.set_size_request(280, -1)
        sidebar.add_css_class("sidebar")
        sidebar.set_margin_top(12)
        sidebar.set_margin_bottom(12)
        sidebar.set_margin_start(12)
        content_box.append(sidebar)
        
        # Create expandable menu sections
        self._create_menu_sections(sidebar)
        
        # Separator
        separator = Gtk.Separator(orientation=Gtk.Orientation.VERTICAL)
        separator.set_margin_top(12)
        separator.set_margin_bottom(12)
        content_box.append(separator)
        
        # Main content area
        main_content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        main_content.set_hexpand(True)
        content_box.append(main_content)
        
        # Main drives title with hamster icon
        self.drives_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.hamster_icon = Gtk.Image.new_from_file("hamster.png")
        self.hamster_icon.set_pixel_size(32)
        self.drives_box.append(self.hamster_icon)
        self.drives_label = Gtk.Label(label="Drives")
        self.drives_label.set_halign(Gtk.Align.START)
        self.drives_label.add_css_class("heading")
        self.drives_box.append(self.drives_label)
        self.drives_box.set_halign(Gtk.Align.START)
        self.drives_box.set_margin_top(12)
        self.drives_box.set_margin_start(12)
        self.drives_box.set_margin_bottom(6)
        main_content.append(self.drives_box)
        
        # Unified drives scrolled area
        scrolled = Gtk.ScrolledWindow()
        scrolled.set_vexpand(True)
        main_content.append(scrolled)
        
        # Unified drive list
        self.drive_list = Gtk.ListBox()
        self.drive_list.set_selection_mode(Gtk.SelectionMode.NONE)
        self.drive_list.add_css_class("boxed-list")
        self.drive_list.set_margin_top(12)
        self.drive_list.set_margin_bottom(12)
        self.drive_list.set_margin_start(12)
        self.drive_list.set_margin_end(12)
        scrolled.set_child(self.drive_list)
    
    def _create_menu_sections(self, sidebar):
        """Create expandable menu sections like in the screenshot"""
        # Only USB Drives, Tools, and Settings sections
        # USB Drives section
        usb_drives_group = Adw.PreferencesGroup()
        usb_drives_group.set_title("USB Drives")
        sidebar.append(usb_drives_group)
        self.usb_drives_menu = usb_drives_group
        
        # Tools section
        tools_group = Adw.PreferencesGroup()
        tools_group.set_title("Tools")
        sidebar.append(tools_group)
        # RAID Management
        raid_row = Adw.ActionRow()
        raid_row.set_title("RAID")
        raid_row.set_subtitle("Manage RAID arrays")
        raid_icon = Gtk.Image.new_from_icon_name("drive-multidisk-symbolic")
        raid_row.add_prefix(raid_icon)
        raid_row.set_activatable(True)
        raid_row.connect("activated", self._open_raid_window)
        tools_group.add(raid_row)
        # Bcache Management
        bcache_row = Adw.ActionRow()
        bcache_row.set_title("Bcache")
        bcache_row.set_subtitle("Manage block cache")
        bcache_icon = Gtk.Image.new_from_icon_name("drive-harddisk-symbolic")
        bcache_row.add_prefix(bcache_icon)
        bcache_row.set_activatable(True)
        bcache_row.connect("activated", self._open_bcache_window)
        tools_group.add(bcache_row)
        # Fstab Wizard
        fstab_row = Adw.ActionRow()
        fstab_row.set_title("Fstab")
        fstab_row.set_subtitle("Persistent mount setup")
        fstab_icon = Gtk.Image.new_from_icon_name("drive-harddisk-symbolic")
        fstab_row.add_prefix(fstab_icon)
        fstab_row.set_activatable(True)
        fstab_row.connect("activated", self._open_fstab_wizard)
        tools_group.add(fstab_row)
        # Benchmark Tool
        bench_row = Adw.ActionRow()
        bench_row.set_title("Benchmark")
        bench_row.set_subtitle("Read/write speed test")
        bench_icon = Gtk.Image.new_from_icon_name("media-playback-start-symbolic")
        bench_row.add_prefix(bench_icon)
        bench_row.set_activatable(True)
        bench_row.connect("activated", self._open_benchmark_wizard)
        tools_group.add(bench_row)
        # SMART tool (moved here)
        smart_row = Adw.ActionRow()
        smart_row.set_title("SMART")
        smart_row.set_subtitle("Run drive self-tests")
        smart_icon = Gtk.Image.new_from_icon_name("drive-harddisk-symbolic")
        smart_row.add_prefix(smart_icon)
        smart_row.set_activatable(True)
        smart_row.connect("activated", self._open_smart_wizard)
        tools_group.add(smart_row)
        # System Info
        info_row = Adw.ActionRow()
        info_row.set_title("System Info")
        info_row.set_subtitle("Hardware details")
        info_icon = Gtk.Image.new_from_icon_name("computer-symbolic")
        info_row.add_prefix(info_icon)
        info_row.set_activatable(True)
        info_row.connect("activated", self._open_system_info_window)
        tools_group.add(info_row)
        # Settings section at bottom (sidebar)
        settings_group = Adw.PreferencesGroup()
        sidebar.append(settings_group)
        settings_row = Adw.ActionRow()
        settings_row.set_title("Settings")
        settings_row.set_subtitle("Application preferences")
        settings_icon = Gtk.Image.new_from_icon_name("preferences-system-symbolic")
        settings_row.add_prefix(settings_icon)
        settings_row.set_activatable(True)
        settings_row.connect("activated", self._open_settings_window)
        tools_group.add(settings_row)
        # Add hamster toggle
        hamster_toggle_row = Adw.ActionRow()
        hamster_toggle_row.set_title("Show Hamster Icon")
        hamster_switch = Gtk.Switch()
        hamster_switch.set_active(self.show_hamster)
        hamster_switch.set_halign(Gtk.Align.END)
        hamster_switch.set_valign(Gtk.Align.CENTER)
        hamster_switch.connect("notify::active", self._on_hamster_toggle)
        hamster_toggle_row.add_suffix(hamster_switch)
        settings_group.add(hamster_toggle_row)
    
    def _open_raid_window(self, row):
        """Open RAID management window"""
        raid_window = RaidWindow(self)
        raid_window.present()
    
    def _open_bcache_window(self, row):
        """Open Bcache management window"""
        bcache_window = BcacheWindow(self)
        bcache_window.present()
    
    def _open_system_info_window(self, row):
        win = SystemInfoWindow(self)
        win.present()
    
    def _on_hamster_toggle(self, switch, param):
        self.show_hamster = switch.get_active()
        if self.hamster_icon.get_parent() is self.drives_box:
            self.drives_box.remove(self.hamster_icon)
        if self.show_hamster:
            self.drives_box.prepend(self.hamster_icon)
    
    def refresh_drives(self):
        """Refresh the drive information"""
        # Clear existing drives from main list
        while child := self.drive_list.get_first_child():
            self.drive_list.remove(child)
        
        # Clear menu items (but keep the groups)
        self._clear_menu_drives()
        
        # Get drives
        all_drives = DriveInfo.get_drives()
        self.main_drives, self.usb_drives = DriveInfo.filter_drives(all_drives)
        
        # Populate unified drive list in content area (show all main drives)
        for drive in self.main_drives:
            drive_row = self._create_drive_row(drive)
            self.drive_list.append(drive_row)
        
        # Populate sidebar menus
        self._populate_menu_drives()
        
        # Show message if no drives
        if not self.main_drives:
            empty_row = Adw.ActionRow()
            empty_row.set_title("No drives found")
            empty_row.set_subtitle("Check if drives are properly connected")
            self.drive_list.append(empty_row)
    
    def _clear_menu_drives(self):
        """Clear drive items from menu groups"""
        # Only clear USB drives menu
        while self.usb_drives_menu.get_first_child():
            child = self.usb_drives_menu.get_first_child()
            if hasattr(child, 'get_title') and child.get_title() not in ["USB Drives"]:
                self.usb_drives_menu.remove(child)
            else:
                break
    
    def _populate_menu_drives(self):
        """Populate drive items in sidebar menus"""
        # Only add USB drives to sidebar
        for drive in self.usb_drives:
            row = Adw.ActionRow()
            row.set_title(drive['display_name'])
            row.set_subtitle(f"{drive['size']} • USB")
            icon = Gtk.Image.new_from_icon_name("drive-removable-media-symbolic")
            row.add_prefix(icon)
            row.set_activatable(True)
            row.connect("activated", lambda r, d=drive: self._show_drive_details(d))
            self.usb_drives_menu.add(row)
        # Show empty state for USB if none
        if not self.usb_drives:
            empty_row = Adw.ActionRow()
            empty_row.set_title("No USB drives")
            empty_row.set_subtitle("Connect a USB drive")
            empty_row.set_sensitive(False)
            self.usb_drives_menu.add(empty_row)
    
    def _get_drive_type_short(self, drive):
        """Get short drive type description"""
        if drive['is_nvme']:
            return "NVMe"
        elif drive['rotational']:
            return "HDD"
        else:
            return "SSD"
    
    def _create_drive_row(self, drive):
        """Create a detailed drive row for the main content area"""
        row = Adw.ActionRow()
        # Title: Model (Device)
        row.set_title(f"{drive['model']} ({drive['name']})")
        
        # Subtitle: Capacity, Type, Transport in all-caps and clear spacing
        type_info = self._get_drive_type_short(drive)
        subtitle = f"{drive['size']}   {type_info.upper()}   {drive['transport'].upper() if drive['transport'] else 'UNKNOWN'}"
        row.set_subtitle(subtitle)
        
        # Details button
        details_btn = Gtk.Button.new_with_label("Details")
        details_btn.set_valign(Gtk.Align.CENTER)
        details_btn.connect("clicked", lambda x, d=drive: self._show_drive_details(d))
        row.add_suffix(details_btn)
        
        return row
    
    def _show_drive_details(self, drive):
        """Show detailed drive information"""
        detail_window = DriveDetailWindow(self, drive)
        detail_window.present()

    # --- Real-time USB detection using pyudev (GLib integration) ---
    def _start_udev_monitor(self):
        if pyudev is None:
            return
        context = pyudev.Context()
        monitor = pyudev.Monitor.from_netlink(context)
        monitor.filter_by(subsystem='block')
        # Use a compatible observer for GTK main loop integration
        try:
            # Try pyudev.glib.MonitorObserver (for most distros)
            from pyudev.glib import MonitorObserver  # type: ignore
            observer = MonitorObserver(monitor)
            # MonitorObserver in pyudev.glib uses 'connect_event' instead of 'connect'
            observer.connect_event('device-event', lambda o, d: self._on_udev_event(d))
            # Do NOT call observer.start() here; pyudev.glib handles it
        except (ImportError, AttributeError):
            try:
                # Fallback: try to use observer.start() if available
                observer = pyudev.MonitorObserver(monitor)
                try:
                    observer.connect('device-event', lambda o, d: self._on_udev_event(d))  # type: ignore
                except AttributeError:
                    print('pyudev.MonitorObserver has no connect method; USB hotplug detection may not work.')
                observer.start()
            except Exception:
                print("pyudev hotplug observer not available; USB hotplug detection disabled.")

    def _on_udev_event(self, device):
        if device.device_type == 'disk' and device.get('ID_BUS') == 'usb':
            self.refresh_drives()

    # --- Helper: Get extra USB info for a device node (for detail view) ---
    def get_usb_device_info(self, devnode):
        """Return a dict of extra USB info for a given /dev/sdX node using pyudev, or None if not found."""
        if pyudev is None:
            return None
        context = pyudev.Context()
        for device in context.list_devices(subsystem='block', DEVNAME=devnode):
            if device.get('ID_BUS') == 'usb':
                return {
                    'serial': device.get('ID_SERIAL_SHORT'),
                    'vendor': device.get('ID_VENDOR'),
                    'model': device.get('ID_MODEL'),
                    'bus': device.get('ID_BUS'),
                    'wwn': device.get('ID_WWN'),
                    'usb_driver': device.get('ID_USB_DRIVER'),
                    'usb_port': device.get('ID_PATH'),
                }
        return None

    def _open_smart_wizard(self, row):
        wizard = SmartWizard(self)
        wizard.present()

    def _open_settings_window(self, row):
        win = SettingsWindow(self)
        win.present()

    def _open_fstab_wizard(self, row):
        wizard = FstabWizard(self)
        wizard.present()

    def _open_benchmark_wizard(self, row):
        wizard = BenchmarkWizard(self)
        wizard.present()


class SmartWizard(Adw.Window):
    """Step-by-step SMART test wizard"""
    def __init__(self, parent):
        super().__init__()
        self.set_transient_for(parent)
        self.set_modal(True)
        self.set_default_size(600, 400)
        self.set_title("SMART Test Wizard")
        self.current_step = 0
        self.selected_drive = None
        self.selected_test = None
        self.smart_info = None
        self._setup_ui()
        self._show_step(0)

    def _setup_ui(self):
        self.stack = Gtk.Stack()
        self.stack.set_transition_type(Gtk.StackTransitionType.SLIDE_LEFT_RIGHT)
        nav_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        nav_box.set_margin_top(12)
        nav_box.set_margin_bottom(12)
        nav_box.set_margin_end(24)
        nav_box.set_halign(Gtk.Align.END)
        self.cancel_btn = Gtk.Button.new_with_label("Cancel")
        self.back_btn = Gtk.Button.new_with_label("Back")
        self.next_btn = Gtk.Button.new_with_label("Next")
        nav_box.append(self.cancel_btn)
        nav_box.append(self.back_btn)
        nav_box.append(self.next_btn)
        main_vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        main_vbox.append(self.stack)
        main_vbox.append(nav_box)
        self.set_content(main_vbox)

        # Step 0: Introduction
        intro_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        intro_box.set_margin_top(24)
        intro_box.set_margin_bottom(24)
        intro_box.set_margin_start(24)
        intro_box.set_margin_end(24)
        title = Gtk.Label()
        title.set_markup("<span size='xx-large' weight='bold'>SMART Test Wizard</span>")
        title.set_halign(Gtk.Align.START)
        intro_box.append(title)
        desc = Gtk.Label(label=(
            "SMART (Self-Monitoring, Analysis, and Reporting Technology) helps you monitor the health of your drives.\n\n"
            "You can run a short or long self-test to check for problems.\n\n"
            "<b>Warning:</b> Running a test may temporarily impact drive performance.\n\n"
            "Not all drives support SMART.\n"
        ))
        desc.set_wrap(True)
        desc.set_halign(Gtk.Align.START)
        desc.set_use_markup(True)
        intro_box.append(desc)
        self.intro_box = intro_box
        self.stack.add_titled(intro_box, "intro", "Introduction")

        # Step 1: Select Drive
        drive_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        drive_box.set_margin_top(24)
        drive_box.set_margin_bottom(24)
        drive_box.set_margin_start(24)
        drive_box.set_margin_end(24)
        drive_title = Gtk.Label()
        drive_title.set_markup("<span size='x-large' weight='bold'>Select Drive</span>")
        drive_title.set_halign(Gtk.Align.START)
        drive_box.append(drive_title)
        drive_desc = Gtk.Label(label="Select a drive with SMART support.")
        drive_desc.set_wrap(True)
        drive_desc.set_halign(Gtk.Align.START)
        drive_box.append(drive_desc)
        self.drive_list = Gtk.ListBox()
        self.drive_list.set_selection_mode(Gtk.SelectionMode.SINGLE)
        drive_box.append(self.drive_list)
        self.drive_box = drive_box
        self.stack.add_titled(drive_box, "drive", "Drive")

        # Step 2: Select Test Type (GTK4 ListBox)
        test_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        test_box.set_margin_top(24)
        test_box.set_margin_bottom(24)
        test_box.set_margin_start(24)
        test_box.set_margin_end(24)
        test_title = Gtk.Label()
        test_title.set_markup("<span size='x-large' weight='bold'>Select Test Type</span>")
        test_title.set_halign(Gtk.Align.START)
        test_box.append(test_title)
        self.test_type_list = Gtk.ListBox()
        self.test_type_list.set_selection_mode(Gtk.SelectionMode.SINGLE)
        for label in ["Short Test (Quick health check, ~2 min)", "Long Test (Comprehensive, 30+ min)"]:
            row = Gtk.ListBoxRow()
            box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
            lbl = Gtk.Label(label=label)
            lbl.set_xalign(0)
            box.append(lbl)
            row.set_child(box)
            self.test_type_list.append(row)
        test_box.append(self.test_type_list)
        # Add detailed info label below the test type list
        info_label = Gtk.Label()
        info_label.set_wrap(True)
        info_label.set_halign(Gtk.Align.START)
        info_label.set_margin_top(18)
        info_label.set_markup(
            "<b>What happens next:</b>\n"
            "When you press <b>Next</b>, a terminal window will open and ask for your <b>sudo password</b> to start the SMART test.\n\n"
            "After the test starts, you will see a <b>Show SMART Data in Terminal</b> button. Click it to open another terminal, enter your password again if prompted, and view the detailed SMART results.\n\n"
            "<b>Note:</b> The SMART data will only be visible in the terminal window, not in the app. You must check the terminal for test progress and results."
        )
        test_box.append(info_label)
        self.test_box = test_box
        self.stack.add_titled(test_box, "test", "Test Type")

        # Step 3: Progress/Result
        result_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        result_box.set_margin_top(24)
        result_box.set_margin_bottom(24)
        result_box.set_margin_start(24)
        result_box.set_margin_end(24)
        self.result_label = Gtk.Label()
        self.result_label.set_wrap(True)
        self.result_label.set_halign(Gtk.Align.START)
        result_box.append(self.result_label)
        self.result_group = Adw.PreferencesGroup()
        result_box.append(self.result_group)
        self.finish_btn = Gtk.Button.new_with_label("Finish")
        self.finish_btn.connect("clicked", lambda x: self.close())
        result_box.append(self.finish_btn)
        self.stack.add_titled(result_box, "result", "Result")
        self.finish_btn.set_sensitive(False)

        self.cancel_btn.connect("clicked", lambda x: self.close())
        self.back_btn.connect("clicked", self._on_back)
        self.next_btn.connect("clicked", self._on_next)

    def _show_step(self, step):
        self.current_step = step
        if step == 0:
            self.stack.set_visible_child(self.intro_box)
            self.back_btn.set_sensitive(False)
            self.next_btn.set_sensitive(True)
            self.next_btn.set_label("Next")
        elif step == 1:
            self._populate_drive_list()
            self.stack.set_visible_child(self.drive_box)
            self.back_btn.set_sensitive(True)
            self.next_btn.set_sensitive(True)
            self.next_btn.set_label("Next")
        elif step == 2:
            self.stack.set_visible_child(self.test_box)
            self.back_btn.set_sensitive(True)
            self.next_btn.set_sensitive(True)
            self.next_btn.set_label("Next")
        elif step == 3:
            self.stack.set_visible_child(self.result_label.get_parent())
            self.back_btn.set_sensitive(False)
            self.next_btn.set_sensitive(False)
            self.finish_btn.set_sensitive(False)

    def _on_next(self, btn):
        if self.current_step == 0:
            self._show_step(1)
        elif self.current_step == 1:
            selected = self.drive_list.get_selected_row()
            if not selected:
                self._show_error("Please select a drive to continue.")
                return
            self.selected_drive = selected.device
            self._show_step(2)
        elif self.current_step == 2:
            selected_row = self.test_type_list.get_selected_row()
            if not selected_row:
                self._show_error("Please select a test type.")
                return
            if selected_row.get_index() == 0:
                self.selected_test = "short"
            else:
                self.selected_test = "long"
            self._show_step(3)
            self._run_smart_test()

    def _on_back(self, btn):
        if self.current_step == 1:
            self._show_step(0)
        elif self.current_step == 2:
            self._show_step(1)
        elif self.current_step == 3:
            self._show_step(2)

    def _populate_drive_list(self):
        while child := self.drive_list.get_first_child():
            self.drive_list.remove(child)
        drives = DriveInfo.get_drives()
        for drive in drives:
            # Show all drives
            row = Gtk.ListBoxRow()
            box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
            label = Gtk.Label(label=f"{drive['model']} ({drive['name']}) - {drive['size']}")
            label.set_xalign(0)
            box.append(label)
            row.set_child(box)
            row.device = drive['name']
            self.drive_list.append(row)

    def _run_smart_test(self):
        self.result_label.set_text("Running SMART test... Please wait.")
        self.finish_btn.set_sensitive(False)
        def worker():
            import time
            success, msg = DriveInfo.run_smart_test(self.selected_drive, self.selected_test)
            time.sleep(2)
            smart = DriveInfo.get_smart_info(self.selected_drive)
            GLib.idle_add(self._show_smart_result, success, msg, smart)
        import threading
        threading.Thread(target=worker, daemon=True).start()

    def _show_smart_result(self, success, msg, smart):
        if success:
            self.result_label.set_markup(f"<span foreground='green' weight='bold'>SMART test started successfully!</span>\n\n{msg}")
        else:
            # Show actual error output if available
            error_msg = msg
            if smart:
                if 'output' in smart and smart['output']:
                    error_msg += f"\n\nSTDOUT:\n{smart['output']}"
                if 'error' in smart and smart['error']:
                    error_msg += f"\n\nERROR:\n{smart['error']}"
            self.result_label.set_markup(f"<span foreground='red' weight='bold'>Failed to start SMART test.</span>\n\n{error_msg}")
        self.finish_btn.set_sensitive(True)
        # Show SMART summary and attributes
        children = [child for child in self.result_group]
        import traceback
        for child in children:
            if child.get_parent() is self.result_group and isinstance(child, Adw.ActionRow):
                self.result_group.remove(child)
            elif isinstance(child, Gtk.Box):
                pass  # Do not remove
            else:
                pass  # Do not remove
        if smart['available'] and smart.get('attributes'):
            summary_row = Adw.ActionRow()
            summary_row.set_title("Health Status")
            summary_row.set_subtitle("Healthy" if smart['health'] else "Warning/Failing")
            self.result_group.add(summary_row)
            for attr in smart['attributes']:
                row = Adw.ActionRow()
                row.set_title(attr.get('name', attr.get('id', '')))
                row.set_subtitle(attr.get('value', attr.get('raw', '')))
                self.result_group.add(row)
        else:
            # Minimal, clean: just a centered label and the button
            info_label = Gtk.Label()
            info_label.set_text("SMART data is only available in the terminal.")
            info_label.set_wrap(True)
            info_label.set_halign(Gtk.Align.CENTER)
            info_label.set_margin_top(12)
        # Add 'Show SMART Data in Terminal' button after test
        if self.selected_drive:
            show_btn_row = Adw.ActionRow()
            show_btn = Gtk.Button.new_with_label("Show SMART Data in Terminal")
            def on_show_smart_terminal(btn):
                import shlex
                term = DriveInfo.detect_terminal()
                if not term:
                    return
                device = self.selected_drive
                if not isinstance(device, str) or not device:
                    return
                smart_cmd = f"sudo smartctl -A {shlex.quote(device)}; read -n 1 -s -r -p 'Press any key to close...'"
                if "gnome-terminal" in term or "xfce4-terminal" in term or "lxterminal" in term:
                    cmd = [term, "--", "bash", "-c", smart_cmd]
                elif "konsole" in term:
                    cmd = [term, "-e", "bash", "-c", smart_cmd]
                else:
                    cmd = [term, "-e", "bash", "-c", smart_cmd]
                try:
                    subprocess.run(cmd, check=True)
                except Exception as e:
                    pass
            show_btn.connect("clicked", on_show_smart_terminal)
            show_btn_row.set_title("")
            show_btn_row.add_suffix(show_btn)
            self.result_group.add(show_btn_row)

    def _show_error(self, message):
        dialog = Adw.MessageDialog(transient_for=self, modal=True)
        dialog.set_heading("Error")
        dialog.set_body(message)
        dialog.add_response("ok", "OK")
        dialog.set_default_response("ok")
        dialog.connect("response", lambda d, r: d.close())
        dialog.present()


class SettingsWindow(Adw.Window):
    def __init__(self, parent):
        super().__init__()
        self.set_transient_for(parent)
        self.set_modal(True)
        self.set_default_size(500, 400)
        self.set_title("Settings & About")
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        box.set_margin_top(18)
        box.set_margin_bottom(18)
        box.set_margin_start(18)
        box.set_margin_end(18)
        # Add close button at the top
        close_btn = Gtk.Button.new_from_icon_name("window-close-symbolic")
        close_btn.set_halign(Gtk.Align.END)
        close_btn.connect("clicked", lambda x: self.close())
        box.append(close_btn)
        # Preferences (add your settings widgets here)
        pref_group = Adw.PreferencesGroup()
        pref_group.set_title("Preferences")
        box.append(pref_group)
        # About section
        about_group = Adw.PreferencesGroup()
        about_group.set_title("About DarkDiskz")
        about_label = Gtk.Label()
        about_label.set_markup(
            "<b>DarkDiskz</b>\n"
            "Version: 1.0.0\n"
            "Creator: ant\n"
            "Advanced open-source disk management for Linux.\n"
            "<a href='https://github.com/ant/DarkDiskz'>GitHub</a>"
        )
        about_label.set_wrap(True)
        about_label.set_halign(Gtk.Align.START)
        about_group.add(about_label)
        box.append(about_group)
        self.set_content(box)

    def _create_menu_sections(self, sidebar):
        """Create expandable menu sections like in the screenshot"""
        # Only USB Drives, Tools, and Settings sections
        # USB Drives section
        usb_drives_group = Adw.PreferencesGroup()
        usb_drives_group.set_title("USB Drives")
        sidebar.append(usb_drives_group)
        self.usb_drives_menu = usb_drives_group
        
        # Tools section
        tools_group = Adw.PreferencesGroup()
        tools_group.set_title("Tools")
        sidebar.append(tools_group)
        # RAID Management
        raid_row = Adw.ActionRow()
        raid_row.set_title("RAID")
        raid_row.set_subtitle("Manage RAID arrays")
        raid_icon = Gtk.Image.new_from_icon_name("drive-multidisk-symbolic")
        raid_row.add_prefix(raid_icon)
        raid_row.set_activatable(True)
        raid_row.connect("activated", self._open_raid_window)
        tools_group.add(raid_row)
        # Bcache Management
        bcache_row = Adw.ActionRow()
        bcache_row.set_title("Bcache")
        bcache_row.set_subtitle("Manage block cache")
        bcache_icon = Gtk.Image.new_from_icon_name("drive-harddisk-symbolic")
        bcache_row.add_prefix(bcache_icon)
        bcache_row.set_activatable(True)
        bcache_row.connect("activated", self._open_bcache_window)
        tools_group.add(bcache_row)
        # Fstab Wizard
        fstab_row = Adw.ActionRow()
        fstab_row.set_title("Fstab")
        fstab_row.set_subtitle("Persistent mount setup")
        fstab_icon = Gtk.Image.new_from_icon_name("drive-harddisk-symbolic")
        fstab_row.add_prefix(fstab_icon)
        fstab_row.set_activatable(True)
        fstab_row.connect("activated", self._open_fstab_wizard)
        tools_group.add(fstab_row)
        # Benchmark Tool
        bench_row = Adw.ActionRow()
        bench_row.set_title("Benchmark")
        bench_row.set_subtitle("Read/write speed test")
        bench_icon = Gtk.Image.new_from_icon_name("media-playback-start-symbolic")
        bench_row.add_prefix(bench_icon)
        bench_row.set_activatable(True)
        bench_row.connect("activated", self._open_benchmark_wizard)
        tools_group.add(bench_row)
        # SMART tool (moved here)
        smart_row = Adw.ActionRow()
        smart_row.set_title("SMART")
        smart_row.set_subtitle("Run drive self-tests")
        smart_icon = Gtk.Image.new_from_icon_name("drive-harddisk-symbolic")
        smart_row.add_prefix(smart_icon)
        smart_row.set_activatable(True)
        smart_row.connect("activated", self._open_smart_wizard)
        tools_group.add(smart_row)
        # System Info
        info_row = Adw.ActionRow()
        info_row.set_title("System Info")
        info_row.set_subtitle("Hardware details")
        info_icon = Gtk.Image.new_from_icon_name("computer-symbolic")
        info_row.add_prefix(info_icon)
        info_row.set_activatable(True)
        info_row.connect("activated", self._open_system_info_window)
        tools_group.add(info_row)
        # Settings section at bottom (sidebar)
        settings_group = Adw.PreferencesGroup()
        sidebar.append(settings_group)
        settings_row = Adw.ActionRow()
        settings_row.set_title("Settings")
        settings_row.set_subtitle("Application preferences")
        settings_icon = Gtk.Image.new_from_icon_name("preferences-system-symbolic")
        settings_row.add_prefix(settings_icon)
        settings_row.set_activatable(True)
        settings_row.connect("activated", self._open_settings_window)
        tools_group.add(settings_row)
        # Add hamster toggle
        hamster_toggle_row = Adw.ActionRow()
        hamster_toggle_row.set_title("Show Hamster Icon")
        hamster_switch = Gtk.Switch()
        hamster_switch.set_active(self.show_hamster)
        hamster_switch.set_halign(Gtk.Align.END)
        hamster_switch.set_valign(Gtk.Align.CENTER)
        hamster_switch.connect("notify::active", self._on_hamster_toggle)
        hamster_toggle_row.add_suffix(hamster_switch)
        settings_group.add(hamster_toggle_row)
    
    def _open_settings_window(self, row):
        win = SettingsWindow(self)
        win.present()

    def _open_fstab_wizard(self, row):
        wizard = FstabWizard(self)
        wizard.present()


class FstabWizard(Adw.Window):
    """Step-by-step fstab setup wizard"""
    def __init__(self, parent):
        super().__init__()
        self.set_transient_for(parent)
        self.set_modal(True)
        self.set_default_size(600, 400)
        self.set_title("Fstab Setup Wizard")
        self.current_step = 0
        self.selected_drive = None
        self.mount_point = None
        self.fs_type = None
        self.mount_options = None
        self.summary_line = None
        self.needs_format = False
        self.format_done = False
        self._setup_ui()
        self._show_step(0)

    def _setup_ui(self):
        self.stack = Gtk.Stack()
        self.stack.set_transition_type(Gtk.StackTransitionType.SLIDE_LEFT_RIGHT)
        nav_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        nav_box.set_margin_top(12)
        nav_box.set_margin_bottom(12)
        nav_box.set_margin_end(24)
        nav_box.set_halign(Gtk.Align.END)
        self.cancel_btn = Gtk.Button.new_with_label("Cancel")
        self.back_btn = Gtk.Button.new_with_label("Back")
        self.next_btn = Gtk.Button.new_with_label("Next")
        nav_box.append(self.cancel_btn)
        nav_box.append(self.back_btn)
        nav_box.append(self.next_btn)
        main_vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        main_vbox.append(self.stack)
        main_vbox.append(nav_box)
        self.set_content(main_vbox)

        # Step 0: Introduction
        intro_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        intro_box.set_margin_top(24)
        intro_box.set_margin_bottom(24)
        intro_box.set_margin_start(24)
        intro_box.set_margin_end(24)
        title = Gtk.Label()
        title.set_markup("<span size='xx-large' weight='bold'>Welcome to the Fstab Setup Wizard</span>")
        title.set_halign(Gtk.Align.START)
        intro_box.append(title)
        desc = Gtk.Label(label=(
            "This wizard will help you set up a persistent mount for any drive, RAID, or Bcache device.\n\n"
            "Drives added to /etc/fstab will be mounted automatically at boot.\n\n"
            "You will be guided step by step and can review all changes before anything is applied."
        ))
        desc.set_wrap(True)
        desc.set_halign(Gtk.Align.START)
        intro_box.append(desc)
        self.intro_box = intro_box
        self.stack.add_titled(intro_box, "intro", "Introduction")

        # Step 1: Select Drive
        drive_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        drive_box.set_margin_top(24)
        drive_box.set_margin_bottom(24)
        drive_box.set_margin_start(24)
        drive_box.set_margin_end(24)
        drive_title = Gtk.Label()
        drive_title.set_markup("<span size='x-large' weight='bold'>Select Drive or Array</span>")
        drive_title.set_halign(Gtk.Align.START)
        drive_box.append(drive_title)
        drive_desc = Gtk.Label(label="Select a drive, RAID, or Bcache device to mount persistently.")
        drive_desc.set_wrap(True)
        drive_desc.set_halign(Gtk.Align.START)
        drive_box.append(drive_desc)
        self.drive_list = Gtk.ListBox()
        self.drive_list.set_selection_mode(Gtk.SelectionMode.SINGLE)
        drive_box.append(self.drive_list)
        self.drive_box = drive_box
        self.stack.add_titled(drive_box, "drive", "Drive")

        # Step 2: Mount Point
        mp_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        mp_box.set_margin_top(24)
        mp_box.set_margin_bottom(24)
        mp_box.set_margin_start(24)
        mp_box.set_margin_end(24)
        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        mp_title = Gtk.Label()
        mp_title.set_markup("<span size='x-large' weight='bold'>Choose Mount Point</span>")
        mp_title.set_halign(Gtk.Align.START)
        vbox.append(mp_title)
        mp_desc = Gtk.Label(label="Select or enter a folder where this drive should be mounted.")
        mp_desc.set_wrap(True)
        mp_desc.set_halign(Gtk.Align.START)
        vbox.append(mp_desc)
        hbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.mp_entry = Gtk.Entry()
        self.mp_entry.set_placeholder_text("e.g. /mnt/data or /media/raid")
        hbox.append(self.mp_entry)
        browse_btn = Gtk.Button.new_with_label("Browse…")
        def on_browse(btn):
            dialog = Gtk.FileChooserDialog(title="Select Mount Point", parent=self, action=Gtk.FileChooserAction.SELECT_FOLDER)
            dialog.add_button("Cancel", Gtk.ResponseType.CANCEL)
            dialog.add_button("Select", Gtk.ResponseType.OK)
            if self.mp_entry.get_text():
                dialog.set_current_folder(self.mp_entry.get_text())
            response = dialog.run()
            if response == Gtk.ResponseType.OK:
                folder = dialog.get_file()
                if folder:
                    self.mp_entry.set_text(folder.get_path())
            dialog.destroy()
        browse_btn.connect("clicked", on_browse)
        hbox.append(browse_btn)
        vbox.append(hbox)
        mp_box.append(vbox)
        self.mp_box = mp_box
        self.stack.add_titled(mp_box, "mountpoint", "Mount Point")

        # Step 3: Filesystem Type
        fs_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        fs_box.set_margin_top(24)
        fs_box.set_margin_bottom(24)
        fs_box.set_margin_start(24)
        fs_box.set_margin_end(24)
        fs_title = Gtk.Label()
        fs_title.set_markup("<span size='x-large' weight='bold'>Filesystem Type</span>")
        fs_title.set_halign(Gtk.Align.START)
        fs_box.append(fs_title)
        fs_desc = Gtk.Label(label="Select the filesystem type for this drive.")
        fs_desc.set_wrap(True)
        fs_desc.set_halign(Gtk.Align.START)
        fs_box.append(fs_desc)
        self.fs_combo = Gtk.ComboBoxText()
        for fs in ["auto", "ext4", "xfs", "btrfs", "ntfs", "vfat"]:
            self.fs_combo.append_text(fs)
        self.fs_combo.set_active(0)
        fs_box.append(self.fs_combo)
        self.fs_box = fs_box
        self.stack.add_titled(fs_box, "fstype", "Filesystem")

        # Step 4: Mount Options
        opt_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        opt_box.set_margin_top(24)
        opt_box.set_margin_bottom(24)
        opt_box.set_margin_start(24)
        opt_box.set_margin_end(24)
        opt_title = Gtk.Label()
        opt_title.set_markup("<span size='x-large' weight='bold'>Mount Options</span>")
        opt_title.set_halign(Gtk.Align.START)
        opt_box.append(opt_title)
        opt_desc = Gtk.Label(label="Choose mount options (comma-separated). Leave blank for defaults.")
        opt_desc.set_wrap(True)
        opt_desc.set_halign(Gtk.Align.START)
        opt_box.append(opt_desc)
        self.opt_entry = Gtk.Entry()
        self.opt_entry.set_placeholder_text("e.g. defaults,noatime")
        opt_box.append(self.opt_entry)
        self.opt_box = opt_box
        self.stack.add_titled(opt_box, "options", "Options")

        # Step 5: Review & Confirm
        review_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        review_box.set_margin_top(24)
        review_box.set_margin_bottom(24)
        review_box.set_margin_start(24)
        review_box.set_margin_end(24)
        review_title = Gtk.Label()
        review_title.set_markup("<span size='x-large' weight='bold'>Review and Confirm</span>")
        review_title.set_halign(Gtk.Align.START)
        review_box.append(review_title)
        self.review_summary = Gtk.Label()
        self.review_summary.set_wrap(True)
        self.review_summary.set_halign(Gtk.Align.START)
        review_box.append(self.review_summary)
        warning = Gtk.Label()
        warning.set_markup("<span foreground='red' weight='bold'>Warning: Incorrect fstab entries can prevent your system from booting!</span>")
        warning.set_halign(Gtk.Align.START)
        review_box.append(warning)
        self.review_box = review_box
        self.stack.add_titled(review_box, "review", "Review")

        # Step 6: Done
        done_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        done_box.set_margin_top(24)
        done_box.set_margin_bottom(24)
        done_box.set_margin_start(24)
        done_box.set_margin_end(24)
        done_label = Gtk.Label()
        done_label.set_markup("<span size='x-large' weight='bold'>Setup Complete</span>")
        done_label.set_halign(Gtk.Align.START)
        done_box.append(done_label)
        self.done_msg = Gtk.Label(label="No changes have been made yet. (This is a preview; actual fstab editing will be implemented next.)")
        self.done_msg.set_wrap(True)
        self.done_msg.set_halign(Gtk.Align.START)
        done_box.append(self.done_msg)
        self.finish_btn = Gtk.Button.new_with_label("Finish")
        self.finish_btn.connect("clicked", lambda x: self.close())
        done_box.append(self.finish_btn)
        self.stack.add_titled(done_box, "done", "Done")
        self.finish_btn.set_sensitive(False)

        self.cancel_btn.connect("clicked", lambda x: self.close())
        self.back_btn.connect("clicked", self._on_back)
        self.next_btn.connect("clicked", self._on_next)

        # Step 4.5: Label (NEW)
        label_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        label_box.set_margin_top(24)
        label_box.set_margin_bottom(24)
        label_box.set_margin_start(24)
        label_box.set_margin_end(24)
        label_title = Gtk.Label()
        label_title.set_markup("<span size='x-large' weight='bold'>Set Volume Label</span>")
        label_title.set_halign(Gtk.Align.START)
        label_box.append(label_title)
        label_desc = Gtk.Label(label="Enter a name for this drive (optional, shown in file manager). Leave blank to skip.")
        label_desc.set_wrap(True)
        label_desc.set_halign(Gtk.Align.START)
        label_box.append(label_desc)
        self.label_entry = Gtk.Entry()
        self.label_entry.set_placeholder_text("e.g. FastRAID, Storage, Games")
        label_box.append(self.label_entry)
        self.label_box = label_box
        self.stack.add_titled(label_box, "label", "Label")

        # Step 1.5: Format Bcache Device (NEW)
        self.format_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        self.format_box.set_margin_top(24)
        self.format_box.set_margin_bottom(24)
        self.format_box.set_margin_start(24)
        self.format_box.set_margin_end(24)
        format_title = Gtk.Label()
        format_title.set_markup("<span size='x-large' weight='bold'>Format Bcache Device</span>")
        format_title.set_halign(Gtk.Align.START)
        self.format_box.append(format_title)
        format_desc = Gtk.Label(label="The selected bcache device does not have a filesystem. You must format it before adding to fstab. All data will be erased.")
        format_desc.set_wrap(True)
        format_desc.set_halign(Gtk.Align.START)
        self.format_box.append(format_desc)
        self.format_btn = Gtk.Button.new_with_label("Format Device")
        self.format_btn.set_halign(Gtk.Align.START)
        self.format_btn.connect("clicked", self._on_format_bcache)
        self.format_box.append(self.format_btn)
        self.format_status = Gtk.Label(label="Not formatted yet.")
        self.format_status.set_halign(Gtk.Align.START)
        self.format_box.append(self.format_status)
        self.stack.add_titled(self.format_box, "format", "Format Bcache")

    def _show_step(self, step):
        self.current_step = step
        if step == 0:
            self.stack.set_visible_child(self.intro_box)
            self.back_btn.set_sensitive(False)
            self.next_btn.set_sensitive(True)
            self.next_btn.set_label("Next")
        elif step == 1:
            self._populate_drive_list()
            self.stack.set_visible_child(self.drive_box)
            self.back_btn.set_sensitive(True)
            self.next_btn.set_sensitive(True)
            self.next_btn.set_label("Next")
        elif step == 1.5:
            self.format_done = False
            self.format_status.set_text("Not formatted yet.")
            self.stack.set_visible_child(self.format_box)
            self.back_btn.set_sensitive(True)
            self.next_btn.set_sensitive(False)
            self.next_btn.set_label("Next")
        elif step == 2:
            # Set default mount point suggestion
            default_mp = "/mnt/data"
            label = None
            name = None
            selected = self.drive_list.get_selected_row()
            if selected and hasattr(selected, 'device'):
                dev = selected.device
                import subprocess, json
                try:
                    result = subprocess.run(["lsblk", "-no", "LABEL", dev], capture_output=True, text=True)
                    label = result.stdout.strip()
                except Exception:
                    label = None
                if not label:
                    name = dev.split("/")[-1]
                if label:
                    default_mp = f"/mnt/{label}"
                elif name:
                    default_mp = f"/mnt/{name}"
            self.mp_entry.set_text(default_mp)
            self.stack.set_visible_child(self.mp_box)
            self.back_btn.set_sensitive(True)
            self.next_btn.set_sensitive(True)
            self.next_btn.set_label("Next")
        elif step == 3:
            self.stack.set_visible_child(self.fs_box)
            self.back_btn.set_sensitive(True)
            self.next_btn.set_sensitive(True)
            self.next_btn.set_label("Next")
        elif step == 4:
            self.stack.set_visible_child(self.opt_box)
            self.back_btn.set_sensitive(True)
            self.next_btn.set_sensitive(True)
            self.next_btn.set_label("Next")
        elif step == 5:
            self.stack.set_visible_child(self.label_box)
            self.back_btn.set_sensitive(True)
            self.next_btn.set_sensitive(True)
            self.next_btn.set_label("Next")
        elif step == 6:
            self._populate_review()
            self.stack.set_visible_child(self.review_box)
            self.back_btn.set_sensitive(True)
            self.next_btn.set_sensitive(True)
            self.next_btn.set_label("Confirm")
        elif step == 7:
            self.stack.set_visible_child(self.done_msg.get_parent())
            self.back_btn.set_sensitive(False)
            self.next_btn.set_sensitive(False)
            self.finish_btn.set_sensitive(True)

    def _on_next(self, btn):
        if self.current_step == 0:
            self._show_step(1)
        elif self.current_step == 1:
            selected = self.drive_list.get_selected_row()
            if not selected:
                self._show_error("Please select a drive to continue.")
                return
            self.selected_drive = selected.device
            # Check if it's a bcache device and needs formatting
            import subprocess
            dev = self.selected_drive
            is_bcache = dev.startswith("/dev/bcache")
            needs_format = False
            if is_bcache:
                try:
                    result = subprocess.run(["lsblk", "-no", "FSTYPE", dev], capture_output=True, text=True)
                    fstype = result.stdout.strip()
                    if not fstype:
                        needs_format = True
                except Exception:
                    needs_format = True
            self.needs_format = needs_format
            if needs_format:
                self._show_step(1.5)
                return
            self._show_step(2)
        elif self.current_step == 1.5:
            if not self.format_done:
                self._show_error("You must format the bcache device before proceeding.")
                return
            self._show_step(2)
        elif self.current_step == 2:
            mp = self.mp_entry.get_text().strip()
            if not mp or not mp.startswith("/"):
                self._show_error("Please enter a valid mount point (must start with /).")
                return
            self.mount_point = mp
            self._show_step(3)
        elif self.current_step == 3:
            self.fs_type = self.fs_combo.get_active_text()
            self._show_step(4)
        elif self.current_step == 4:
            self.mount_options = self.opt_entry.get_text().strip() or "defaults"
            self._show_step(5)
        elif self.current_step == 5:
            self.volume_label = self.label_entry.get_text().strip()
            self._show_step(6)
        elif self.current_step == 6:
            # Actually perform the fstab edit and mount in a terminal
            import shlex, subprocess
            term = DriveInfo.detect_terminal()
            if not term:
                self._show_error("No supported terminal emulator found. Please install gnome-terminal, xterm, or similar.")
                return
            device = self.selected_drive
            mp = self.mount_point
            fs = self.fs_type
            opts = self.mount_options
            label = getattr(self, 'volume_label', None)
            # Get UUID for fstab
            try:
                blkid = subprocess.run(["blkid", "-s", "UUID", "-o", "value", str(device)], capture_output=True, text=True)
                uuid = blkid.stdout.strip()
            except Exception:
                uuid = None
            if uuid:
                fstab_line = f"UUID={uuid}\t{mp}\t{fs}\t{opts}\t0 2"
            else:
                fstab_line = f"{device}\t{mp}\t{fs}\t{opts}\t0 2"
            # Compose shell commands
            cmds = []
            # Format with label if needed and label is a non-empty string
            if isinstance(label, str) and label and fs in ("ext4", "xfs", "btrfs") and isinstance(device, str) and device:
                if fs == "ext4":
                    cmds.append(f"sudo mkfs.ext4 -L {shlex.quote(label)} {shlex.quote(device)}")
                elif fs == "xfs":
                    cmds.append(f"sudo mkfs.xfs -L {shlex.quote(label)} {shlex.quote(device)}")
                elif fs == "btrfs":
                    cmds.append(f"sudo mkfs.btrfs -L {shlex.quote(label)} {shlex.quote(device)}")
            # Create mount point
            if isinstance(mp, str) and mp:
                cmds.append(f"sudo mkdir -p {shlex.quote(mp)}")
            # Append to fstab
            cmds.append(f"echo '{fstab_line}' | sudo tee -a /etc/fstab")
            # Mount
            cmds.append("sudo mount -a")
            shell_cmd = "; ".join(cmds) + "; read -n 1 -s -r -p 'Press any key to close...'"
            if isinstance(term, str) and all(isinstance(x, str) for x in [term, shell_cmd]):
                if "gnome-terminal" in term or "xfce4-terminal" in term or "lxterminal" in term:
                    cmd = [term, "--", "bash", "-c", shell_cmd]
                elif "konsole" in term:
                    cmd = [term, "-e", "bash", "-c", shell_cmd]
                else:
                    cmd = [term, "-e", "bash", "-c", shell_cmd]
                try:
                    subprocess.run(cmd, check=True)
                    self.done_msg.set_text("Fstab entry added and drive mounted! You can now use your drive.")
                except Exception as e:
                    self.done_msg.set_text(f"Failed: {e}")
            else:
                self.done_msg.set_text("Failed to launch terminal: invalid command.")
            self._show_step(7)

    def _on_back(self, btn):
        if self.current_step == 1:
            self._show_step(0)
        elif self.current_step == 2:
            self._show_step(1)
        elif self.current_step == 3:
            self._show_step(2)
        elif self.current_step == 4:
            self._show_step(3)
        elif self.current_step == 5:
            self._show_step(4)

    def _populate_drive_list(self):
        while child := self.drive_list.get_first_child():
            self.drive_list.remove(child)
        # List all drives, RAID, and Bcache devices
        drives = DriveInfo.get_drives()
        # Add RAID arrays
        if hasattr(RaidManager, 'get_raid_arrays'):
            for arr in RaidManager.get_raid_arrays():
                row = Gtk.ListBoxRow()
                box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
                label = Gtk.Label(label=f"{arr['name']} (RAID {arr['level']})")
                label.set_xalign(0)
                box.append(label)
                row.set_child(box)
                row.device = arr['name']
                self.drive_list.append(row)
        # Add Bcache devices from lsblk
        bcache_names = set()
        if hasattr(BcacheManager, 'get_bcache_devices'):
            for bdev in BcacheManager.get_bcache_devices():
                devname = f"/dev/{bdev['name']}"
                bcache_names.add(devname)
                row = Gtk.ListBoxRow()
                box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
                label = Gtk.Label(label=f"{devname} (Bcache)")
                label.set_xalign(0)
                box.append(label)
                row.set_child(box)
                row.device = devname
                self.drive_list.append(row)
        # Add all /dev/bcache* devices that exist (avoid duplicates)
        import glob, os
        for path in glob.glob("/dev/bcache*"):
            if os.path.exists(path) and path not in bcache_names:
                row = Gtk.ListBoxRow()
                box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
                label = Gtk.Label(label=f"{path} (Bcache)")
                label.set_xalign(0)
                box.append(label)
                row.set_child(box)
                row.device = path
                self.drive_list.append(row)
        # Add regular drives
        for drive in drives:
            row = Gtk.ListBoxRow()
            box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
            label = Gtk.Label(label=f"{drive['model']} ({drive['name']}) - {drive['size']}")
            label.set_xalign(0)
            box.append(label)
            row.set_child(box)
            row.device = drive['name']
            self.drive_list.append(row)

    def _populate_review(self):
        # Compose fstab line (UUID or device)
        device = self.selected_drive
        mp = self.mount_point
        fs = self.fs_type
        opts = self.mount_options
        # In a real implementation, use UUID for safety
        fstab_line = f"{device}\t{mp}\t{fs}\t{opts}\t0 2"
        self.summary_line = fstab_line
        summary = f"Device: {device}\nMount point: {mp}\nFilesystem: {fs}\nOptions: {opts}\n\n<tt>{fstab_line}</tt>"
        self.review_summary.set_markup(summary)

    def _show_error(self, message):
        dialog = Adw.MessageDialog(transient_for=self, modal=True)
        dialog.set_heading("Error")
        dialog.set_body(message)
        dialog.add_response("ok", "OK")
        dialog.set_default_response("ok")
        dialog.connect("response", lambda d, r: d.close())
        dialog.present()

    def _on_format_bcache(self, btn):
        import shlex, subprocess
        term = DriveInfo.detect_terminal()
        if not term:
            self._show_error("No supported terminal emulator found. Please install gnome-terminal, xterm, or similar.")
            return
        dev = self.selected_drive
        if not isinstance(dev, str) or not dev:
            dev = "/dev/null"  # fallback, should not happen
        fs = self.fs_combo.get_active_text() if hasattr(self, 'fs_combo') else "ext4"
        if not isinstance(fs, str) or not fs:
            fs = "ext4"
        mkfs_cmd = f"sudo mkfs.{shlex.quote(fs)} {shlex.quote(dev)}; read -n 1 -s -r -p 'Press any key to close...'"
        if "gnome-terminal" in term or "xfce4-terminal" in term or "lxterminal" in term:
            cmd = [term, "--", "bash", "-c", mkfs_cmd]
        elif "konsole" in term:
            cmd = [term, "-e", "bash", "-c", mkfs_cmd]
        else:
            cmd = [term, "-e", "bash", "-c", mkfs_cmd]
        try:
            subprocess.run(cmd, check=True)
            self.format_done = True
            self.format_status.set_text("Device formatted.")
            self.next_btn.set_sensitive(True)
        except Exception as e:
            self.format_status.set_text(f"Failed: {e}")


class BenchmarkResultsHelper:
    CONFIG_DIR = os.path.expanduser("~/.config/darkdiskz")
    RESULTS_FILE = os.path.join(CONFIG_DIR, "benchmarks.json")

    @staticmethod
    def load_results():
        if not os.path.exists(BenchmarkResultsHelper.RESULTS_FILE):
            return []
        try:
            with open(BenchmarkResultsHelper.RESULTS_FILE, "r") as f:
                return pyjson.load(f)
        except Exception:
            return []

    @staticmethod
    def save_result(result):
        results = BenchmarkResultsHelper.load_results()
        results.append(result)
        os.makedirs(BenchmarkResultsHelper.CONFIG_DIR, exist_ok=True)
        with open(BenchmarkResultsHelper.RESULTS_FILE, "w") as f:
            pyjson.dump(results, f, indent=2)

    @staticmethod
    def get_results_for_device(device):
        results = BenchmarkResultsHelper.load_results()
        return [r for r in results if r.get("device") == device]


class BenchmarkWizard(Adw.Window):
    """Step-by-step disk benchmark wizard"""
    def __init__(self, parent):
        super().__init__()
        self.set_transient_for(parent)
        self.set_modal(True)
        self.set_default_size(600, 400)
        self.set_title("Disk Benchmark Wizard")
        self.current_step = 0
        self.selected_drive = None
        self.test_type = None
        self.test_options = {}
        self.result = None
        self._setup_ui()
        self._show_step(0)

    def _setup_ui(self):
        self.stack = Gtk.Stack()
        self.stack.set_transition_type(Gtk.StackTransitionType.SLIDE_LEFT_RIGHT)
        nav_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        nav_box.set_margin_top(12)
        nav_box.set_margin_bottom(12)
        nav_box.set_margin_end(24)
        nav_box.set_halign(Gtk.Align.END)
        self.cancel_btn = Gtk.Button.new_with_label("Cancel")
        self.back_btn = Gtk.Button.new_with_label("Back")
        self.next_btn = Gtk.Button.new_with_label("Next")
        nav_box.append(self.cancel_btn)
        nav_box.append(self.back_btn)
        nav_box.append(self.next_btn)
        main_vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        main_vbox.append(self.stack)
        main_vbox.append(nav_box)
        self.set_content(main_vbox)

        # Step 0: Introduction
        intro_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        intro_box.set_margin_top(24)
        intro_box.set_margin_bottom(24)
        intro_box.set_margin_start(24)
        intro_box.set_margin_end(24)
        title = Gtk.Label()
        title.set_markup("<span size='xx-large' weight='bold'>Disk Benchmark Wizard</span>")
        title.set_halign(Gtk.Align.START)
        intro_box.append(title)
        desc = Gtk.Label(label=(
            "This wizard will help you test the read and write speed of any drive, RAID, or Bcache device.\n\n"
            "You can compare results before and after RAID/Bcache setup.\n\n"
            "<b>Warning:</b> Write tests may overwrite data. Only run on empty or test drives!"
        ))
        desc.set_wrap(True)
        desc.set_halign(Gtk.Align.START)
        desc.set_use_markup(True)
        intro_box.append(desc)
        self.intro_box = intro_box
        self.stack.add_titled(intro_box, "intro", "Introduction")

        # Step 1: Select Drive
        drive_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        drive_box.set_margin_top(24)
        drive_box.set_margin_bottom(24)
        drive_box.set_margin_start(24)
        drive_box.set_margin_end(24)
        drive_title = Gtk.Label()
        drive_title.set_markup("<span size='x-large' weight='bold'>Select Drive or Array</span>")
        drive_title.set_halign(Gtk.Align.START)
        drive_box.append(drive_title)
        drive_desc = Gtk.Label(label="Select a drive, RAID, or Bcache device to benchmark.")
        drive_desc.set_wrap(True)
        drive_desc.set_halign(Gtk.Align.START)
        drive_box.append(drive_desc)
        self.drive_list = Gtk.ListBox()
        self.drive_list.set_selection_mode(Gtk.SelectionMode.SINGLE)
        drive_box.append(self.drive_list)
        self.drive_box = drive_box
        self.stack.add_titled(drive_box, "drive", "Drive")

        # Step 2: Test Type
        type_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        type_box.set_margin_top(24)
        type_box.set_margin_bottom(24)
        type_box.set_margin_start(24)
        type_box.set_margin_end(24)
        type_title = Gtk.Label()
        type_title.set_markup("<span size='x-large' weight='bold'>Select Test Type</span>")
        type_title.set_halign(Gtk.Align.START)
        type_box.append(type_title)
        self.type_list = Gtk.ListBox()
        self.type_list.set_selection_mode(Gtk.SelectionMode.SINGLE)
        for label in ["Sequential Read", "Sequential Write", "Random Read", "Random Write"]:
            row = Gtk.ListBoxRow()
            box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
            lbl = Gtk.Label(label=label)
            lbl.set_xalign(0)
            box.append(lbl)
            row.set_child(box)
            self.type_list.append(row)
        type_box.append(self.type_list)
        self.type_box = type_box
        self.stack.add_titled(type_box, "type", "Test Type")

        # Step 3: Test Options
        opt_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        opt_box.set_margin_top(24)
        opt_box.set_margin_bottom(24)
        opt_box.set_margin_start(24)
        opt_box.set_margin_end(24)
        opt_title = Gtk.Label()
        opt_title.set_markup("<span size='x-large' weight='bold'>Test Options</span>")
        opt_title.set_halign(Gtk.Align.START)
        opt_box.append(opt_title)
        # File size
        size_label = Gtk.Label(label="Test file size (e.g. 1G, 100M):")
        size_label.set_halign(Gtk.Align.START)
        opt_box.append(size_label)
        self.size_entry = Gtk.Entry()
        self.size_entry.set_text("1G")
        opt_box.append(self.size_entry)
        # Duration
        dur_label = Gtk.Label(label="Test duration (seconds, e.g. 10):")
        dur_label.set_halign(Gtk.Align.START)
        opt_box.append(dur_label)
        self.dur_entry = Gtk.Entry()
        self.dur_entry.set_text("10")
        opt_box.append(self.dur_entry)
        self.opt_box = opt_box
        self.stack.add_titled(opt_box, "options", "Options")

        # Step 4: Review
        review_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        review_box.set_margin_top(24)
        review_box.set_margin_bottom(24)
        review_box.set_margin_start(24)
        review_box.set_margin_end(24)
        review_title = Gtk.Label()
        review_title.set_markup("<span size='x-large' weight='bold'>Review and Confirm</span>")
        review_title.set_halign(Gtk.Align.START)
        review_box.append(review_title)
        self.review_summary = Gtk.Label()
        self.review_summary.set_wrap(True)
        self.review_summary.set_halign(Gtk.Align.START)
        review_box.append(self.review_summary)
        warning = Gtk.Label()
        warning.set_markup("<span foreground='red' weight='bold'>Warning: Write tests may overwrite data. Only run on empty or test drives!</span>")
        warning.set_halign(Gtk.Align.START)
        review_box.append(warning)
        self.review_box = review_box
        self.stack.add_titled(review_box, "review", "Review")

        # Step 5: Results
        result_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        result_box.set_margin_top(24)
        result_box.set_margin_bottom(24)
        result_box.set_margin_start(24)
        result_box.set_margin_end(24)
        result_title = Gtk.Label()
        result_title.set_markup("<span size='x-large' weight='bold'>Benchmark Results</span>")
        result_title.set_halign(Gtk.Align.START)
        result_box.append(result_title)
        self.result_label = Gtk.Label()
        self.result_label.set_wrap(True)
        self.result_label.set_halign(Gtk.Align.START)
        result_box.append(self.result_label)
        # Previous runs
        prev_label = Gtk.Label(label="Previous runs for this device:")
        prev_label.set_halign(Gtk.Align.START)
        result_box.append(prev_label)
        self.prev_runs_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        result_box.append(self.prev_runs_box)
        self.finish_btn = Gtk.Button.new_with_label("Finish")
        self.finish_btn.connect("clicked", lambda x: self.close())
        result_box.append(self.finish_btn)
        self.stack.add_titled(result_box, "result", "Results")
        self.finish_btn.set_sensitive(False)

        self.cancel_btn.connect("clicked", lambda x: self.close())
        self.back_btn.connect("clicked", self._on_back)
        self.next_btn.connect("clicked", self._on_next)

    def _show_step(self, step):
        self.current_step = step
        if step == 0:
            self.stack.set_visible_child(self.intro_box)
            self.back_btn.set_sensitive(False)
            self.next_btn.set_sensitive(True)
            self.next_btn.set_label("Next")
        elif step == 1:
            self._populate_drive_list()
            self.stack.set_visible_child(self.drive_box)
            self.back_btn.set_sensitive(True)
            self.next_btn.set_sensitive(True)
            self.next_btn.set_label("Next")
        elif step == 2:
            self.stack.set_visible_child(self.type_box)
            self.back_btn.set_sensitive(True)
            self.next_btn.set_sensitive(True)
            self.next_btn.set_label("Next")
        elif step == 3:
            self.stack.set_visible_child(self.opt_box)
            self.back_btn.set_sensitive(True)
            self.next_btn.set_sensitive(True)
            self.next_btn.set_label("Next")
        elif step == 4:
            self._populate_review()
            self.stack.set_visible_child(self.review_box)
            self.back_btn.set_sensitive(True)
            self.next_btn.set_sensitive(True)
            self.next_btn.set_label("Run Test")
        elif step == 5:
            self._populate_results()
            self.stack.set_visible_child(self.result_label.get_parent())
            self.back_btn.set_sensitive(False)
            self.next_btn.set_sensitive(False)
            self.finish_btn.set_sensitive(True)

    def _on_next(self, btn):
        if self.current_step == 0:
            self._show_step(1)
        elif self.current_step == 1:
            selected = self.drive_list.get_selected_row()
            if not selected:
                self._show_error("Please select a drive to continue.")
                return
            self.selected_drive = selected.device
            self._show_step(2)
        elif self.current_step == 2:
            selected_row = self.type_list.get_selected_row()
            if not selected_row:
                self._show_error("Please select a test type.")
                return
            self.test_type = selected_row.get_index()
            self._show_step(3)
        elif self.current_step == 3:
            self.test_options = {
                "size": self.size_entry.get_text().strip(),
                "duration": self.dur_entry.get_text().strip()
            }
            self._show_step(4)
        elif self.current_step == 4:
            # Actually run the test using dd in a terminal
            import shlex, subprocess, tempfile, os
            # Determine mount point for the selected drive
            mountpoint = None
            # Use lsblk to find mountpoint
            if not self.selected_drive or not isinstance(self.selected_drive, str):
                self._show_error("No valid drive selected.")
                return
            try:
                result = subprocess.run(["lsblk", "-no", "MOUNTPOINT", self.selected_drive], capture_output=True, text=True)
                mountpoint = result.stdout.strip().splitlines()[0] if result.stdout.strip() else None
            except Exception:
                mountpoint = None
            if not mountpoint or not os.path.isdir(mountpoint):
                self._show_error("Could not determine a valid mount point for this drive. Please ensure it is mounted.")
                return
            testfile = os.path.join(mountpoint, "darkdiskz_benchmark.tmp")
            size = self.test_options.get("size", "1G")
            bs = "1M"
            count = str(int(float(size[:-1]) * 1024)) if size.lower().endswith("g") else str(int(float(size[:-1])))
            if size.lower().endswith("g"):
                count = str(int(float(size[:-1]) * 1024))
            elif size.lower().endswith("m"):
                count = str(int(float(size[:-1])))
            else:
                count = "1024"  # fallback 1G
            # Compose dd command
            if self.test_type == 0:  # Sequential Read
                # First ensure file exists (if not, create it)
                if not os.path.exists(testfile):
                    subprocess.run(["dd", "if=/dev/zero", f"of={testfile}", "bs=1M", f"count={count}", "oflag=direct"], capture_output=True)
                dd_cmd = f"dd if={shlex.quote(testfile)} of=/dev/null bs=1M iflag=direct status=progress"
            elif self.test_type == 1:  # Sequential Write
                dd_cmd = f"dd if=/dev/zero of={shlex.quote(testfile)} bs=1M count={count} oflag=direct status=progress"
            else:
                self._show_error("Only sequential read/write tests are supported in this version.")
                return
            # Terminal integration
            term = DriveInfo.detect_terminal()
            if not term or not isinstance(term, str):
                self._show_error("No supported terminal emulator found. Please install gnome-terminal, xterm, or similar.")
                return
            # Compose command to run in terminal and pause for user to see output
            shell_cmd = f"sudo {dd_cmd}; sync; rm -f {shlex.quote(testfile)}; read -n 1 -s -r -p 'Press any key to close...'"
            if "gnome-terminal" in term or "xfce4-terminal" in term or "lxterminal" in term:
                cmd = [term, "--", "bash", "-c", shell_cmd]
            elif "konsole" in term:
                cmd = [term, "-e", "bash", "-c", shell_cmd]
            else:
                cmd = [term, "-e", "bash", "-c", shell_cmd]
            # Ensure all elements in cmd are strings
            cmd = [str(x) for x in cmd]
            # Run the command in terminal
            try:
                subprocess.run(cmd, check=True)
            except Exception as e:
                self._show_error(f"Failed to launch terminal: {e}")
                return
            # Parse dd output for MB/s (not available here, so just show a placeholder)
            now = datetime.datetime.now().isoformat()
            result = {
                "timestamp": now,
                "device": self.selected_drive,
                "test_type": self.type_list.get_selected_row().get_child().get_first_child().get_text(),
                "options": self.test_options,
                "summary": "(See terminal for actual speed)",
                "raw_output": "(See terminal for dd output)"
            }
            BenchmarkResultsHelper.save_result(result)
            self.result = result
            self._show_step(5)

    def _on_back(self, btn):
        if self.current_step == 1:
            self._show_step(0)
        elif self.current_step == 2:
            self._show_step(1)
        elif self.current_step == 3:
            self._show_step(2)
        elif self.current_step == 4:
            self._show_step(3)

    def _populate_drive_list(self):
        while child := self.drive_list.get_first_child():
            self.drive_list.remove(child)
        # List all drives, RAID, and Bcache devices
        drives = DriveInfo.get_drives()
        # Add RAID arrays
        if hasattr(RaidManager, 'get_raid_arrays'):
            for arr in RaidManager.get_raid_arrays():
                row = Gtk.ListBoxRow()
                box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
                label = Gtk.Label(label=f"{arr['name']} (RAID {arr['level']})")
                label.set_xalign(0)
                box.append(label)
                row.set_child(box)
                row.device = arr['name']
                self.drive_list.append(row)
        # Add Bcache devices from lsblk
        bcache_names = set()
        if hasattr(BcacheManager, 'get_bcache_devices'):
            for bdev in BcacheManager.get_bcache_devices():
                devname = f"/dev/{bdev['name']}"
                bcache_names.add(devname)
                row = Gtk.ListBoxRow()
                box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
                label = Gtk.Label(label=f"{devname} (Bcache)")
                label.set_xalign(0)
                box.append(label)
                row.set_child(box)
                row.device = devname
                self.drive_list.append(row)
        # Add all /dev/bcache* devices that exist (avoid duplicates)
        import glob, os
        for path in glob.glob("/dev/bcache*"):
            if os.path.exists(path) and path not in bcache_names:
                row = Gtk.ListBoxRow()
                box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
                label = Gtk.Label(label=f"{path} (Bcache)")
                label.set_xalign(0)
                box.append(label)
                row.set_child(box)
                row.device = path
                self.drive_list.append(row)
        # Add regular drives
        for drive in drives:
            row = Gtk.ListBoxRow()
            box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
            label = Gtk.Label(label=f"{drive['model']} ({drive['name']}) - {drive['size']}")
            label.set_xalign(0)
            box.append(label)
            row.set_child(box)
            row.device = drive['name']
            self.drive_list.append(row)

    def _populate_review(self):
        summary = f"Device: {self.selected_drive}\nTest type: {self.type_list.get_selected_row().get_child().get_first_child().get_text()}\nOptions: {self.test_options}\n\nThe test will use a file at the drive's mount point. Results will be shown in the terminal window."
        self.review_summary.set_text(summary)

    def _populate_results(self):
        if not self.result:
            self.result_label.set_text("No result.")
            return
        self.result_label.set_text(f"Test: {self.result['test_type']}\nDevice: {self.result['device']}\nOptions: {self.result['options']}\nSummary: {self.result['summary']}")
        # Show previous runs
        for child in self.prev_runs_box:
            self.prev_runs_box.remove(child)
        prev = BenchmarkResultsHelper.get_results_for_device(self.result['device'])
        for r in reversed(prev[-5:]):
            row = Gtk.Label(label=f"{r['timestamp']}: {r['test_type']} {r['options']} {r['summary']}")
            row.set_halign(Gtk.Align.START)
            self.prev_runs_box.append(row)

    def _show_error(self, message):
        dialog = Adw.MessageDialog(transient_for=self, modal=True)
        dialog.set_heading("Error")
        dialog.set_body(message)
        dialog.add_response("ok", "OK")
        dialog.set_default_response("ok")
        dialog.connect("response", lambda d, r: d.close())
        dialog.present()

    def _open_benchmark_wizard(self, row):
        wizard = BenchmarkWizard(self)
        wizard.present()


if __name__ == "__main__":
    class DarkDiskzApp(Adw.Application):
        def __init__(self):
            super().__init__(application_id="com.example.DarkDiskz")
        
        def do_activate(self):
            win = MainWindow(self)
            win.present()

    app = DarkDiskzApp()
    app.run([])



































































































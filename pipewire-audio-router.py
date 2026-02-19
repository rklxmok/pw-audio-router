#!/usr/bin/env python3
"""PipeWire Audio Router - System tray app for routing application audio."""

import json
import os
import re
import subprocess
import sys
import time

from PyQt6.QtCore import QTimer
from PyQt6.QtGui import QAction, QCursor, QIcon
from PyQt6.QtWidgets import QApplication, QMenu, QSystemTrayIcon


class PipeWireManager:
    """Manages PipeWire virtual sinks and audio routing."""

    VIRTUAL_SINK_NAME = "ScreenShareAudio"
    VIRTUAL_MIC_NAME = "VirtualMicrophone"

    def __init__(self):
        self._virtual_sink_id = None
        self._virtual_mic_module = None  # pactl module index
        self._original_default_source = None  # saved before swapping to virtual mic
        self._mic_passthrough_links = []  # (src, dst) pairs for real mic -> virtual mic
        self._tracked_ports = []  # list of link dicts we created
        self._routing_rules = []  # list of {"src_name": ..., "dst_node": ...}
        # Check if we already have virtual nodes from a previous session
        self._detect_existing_virtual_sink()
        self._detect_existing_virtual_mic()

    # ------------------------------------------------------------------
    # Virtual sink management
    # ------------------------------------------------------------------

    def _detect_existing_virtual_sink(self):
        """Find an existing ScreenShareAudio sink from a previous run."""
        try:
            result = subprocess.run(
                ["pw-cli", "ls", "Node"],
                capture_output=True, text=True, timeout=5,
            )
            current_id = None
            for line in result.stdout.splitlines():
                id_match = re.match(r'\s*id (\d+),', line)
                if id_match:
                    current_id = id_match.group(1)
                if self.VIRTUAL_SINK_NAME in line and current_id:
                    self._virtual_sink_id = int(current_id)
                    break
        except (subprocess.SubprocessError, ValueError):
            pass

    def create_virtual_sink(self):
        """Create a virtual PipeWire null sink for screen share audio."""
        if self._virtual_sink_id is not None:
            return True

        props = json.dumps({
            "factory.name": "support.null-audio-sink",
            "node.name": self.VIRTUAL_SINK_NAME,
            "node.description": "Screen Share Audio",
            "media.class": "Audio/Sink",
            "audio.position": "FL,FR",
            "monitor.channel-volumes": "true",
            "monitor.passthrough": "true",
        })

        try:
            result = subprocess.run(
                ["pw-cli", "create-node", "adapter", props],
                capture_output=True, text=True, timeout=5,
            )
            # Parse the node id from output
            for line in result.stdout.splitlines():
                id_match = re.search(r'id:\s*(\d+)', line)
                if id_match:
                    self._virtual_sink_id = int(id_match.group(1))
                    return True
            # Fallback: try to find it by name
            self._detect_existing_virtual_sink()
            return self._virtual_sink_id is not None
        except subprocess.SubprocessError:
            return False

    def destroy_virtual_sink(self):
        """Remove the virtual sink."""
        if self._virtual_sink_id is None:
            return
        # Remove all links that target the virtual sink first
        self.destroy_all_links()
        try:
            subprocess.run(
                ["pw-cli", "destroy", str(self._virtual_sink_id)],
                capture_output=True, text=True, timeout=5,
            )
        except subprocess.SubprocessError:
            pass
        self._virtual_sink_id = None

    @property
    def virtual_sink_active(self):
        return self._virtual_sink_id is not None

    # ------------------------------------------------------------------
    # Virtual microphone management
    # ------------------------------------------------------------------

    def _detect_existing_virtual_mic(self):
        """Find an existing VirtualMicrophone module from a previous run."""
        try:
            result = subprocess.run(
                ["pactl", "list", "modules", "short"],
                capture_output=True, text=True, timeout=5,
            )
            for line in result.stdout.splitlines():
                if self.VIRTUAL_MIC_NAME in line and "module-null-sink" in line:
                    self._virtual_mic_module = line.split()[0]
                    break
        except (subprocess.SubprocessError, ValueError):
            pass

    def create_virtual_mic(self):
        """Create a virtual mic, route the real mic through it, and set as default."""
        if self._virtual_mic_module is not None:
            return True

        # Save the current default source so we can restore it later
        try:
            result = subprocess.run(
                ["pactl", "get-default-source"],
                capture_output=True, text=True, timeout=5,
            )
            self._original_default_source = result.stdout.strip() or None
        except subprocess.SubprocessError:
            self._original_default_source = None

        # Create the virtual mic node
        try:
            result = subprocess.run(
                [
                    "pactl", "load-module", "module-null-sink",
                    "media.class=Audio/Source/Virtual",
                    f"sink_name={self.VIRTUAL_MIC_NAME}",
                    'sink_properties=node.description="Virtual Microphone"',
                ],
                capture_output=True, text=True, timeout=5,
            )
            module_id = result.stdout.strip()
            if not (result.returncode == 0 and module_id):
                return False
            self._virtual_mic_module = module_id
        except subprocess.SubprocessError:
            return False

        # Wait for PipeWire to register the new source before linking/setting default
        time.sleep(0.5)

        # Route the real mic through the virtual mic so the user's voice
        # is always present alongside any routed audio.
        self._setup_mic_passthrough()

        # Set the virtual mic as the default source so apps that don't
        # allow choosing a mic will use it automatically.
        try:
            subprocess.run(
                ["pactl", "set-default-source", self.VIRTUAL_MIC_NAME],
                capture_output=True, text=True, timeout=5,
            )
        except subprocess.SubprocessError:
            pass

        return True

    def _setup_mic_passthrough(self):
        """Link the real default mic's capture ports to the virtual mic's input ports."""
        if not self._original_default_source:
            return

        mic_node = self._find_node_ports("output", self._original_default_source)
        vmic_node = self._find_node_ports("input", self.VIRTUAL_MIC_NAME)
        if not mic_node or not vmic_node:
            return

        mic_ports = [p for p in mic_node["ports"] if ":capture_" in p[1]]
        vmic_ports = vmic_node["ports"]
        if not mic_ports or not vmic_ports:
            return

        # If the real mic is mono, link it to all virtual mic channels
        for i in range(len(vmic_ports)):
            src = mic_ports[min(i, len(mic_ports) - 1)][1]
            dst = vmic_ports[i][1]
            try:
                subprocess.run(
                    ["pw-link", src, dst],
                    capture_output=True, text=True, timeout=5,
                )
                self._mic_passthrough_links.append((src, dst))
            except subprocess.SubprocessError:
                pass

    def destroy_virtual_mic(self):
        """Remove the virtual mic and restore the original default source."""
        if self._virtual_mic_module is None:
            return

        # Remove mic passthrough links
        for src, dst in self._mic_passthrough_links:
            try:
                subprocess.run(
                    ["pw-link", "-d", src, dst],
                    capture_output=True, text=True, timeout=5,
                )
            except subprocess.SubprocessError:
                pass
        self._mic_passthrough_links.clear()

        # Remove any routed audio links
        self.destroy_all_links()

        # Restore the original default source
        if self._original_default_source:
            try:
                subprocess.run(
                    ["pactl", "set-default-source", self._original_default_source],
                    capture_output=True, text=True, timeout=5,
                )
            except subprocess.SubprocessError:
                pass
            self._original_default_source = None

        # Unload the module
        try:
            subprocess.run(
                ["pactl", "unload-module", str(self._virtual_mic_module)],
                capture_output=True, text=True, timeout=5,
            )
        except subprocess.SubprocessError:
            pass
        self._virtual_mic_module = None

    @property
    def virtual_mic_active(self):
        return self._virtual_mic_module is not None

    # ------------------------------------------------------------------
    # Listing sources and destinations
    # ------------------------------------------------------------------

    # Nodes to always hide from menus
    _SKIP_NODES = {"Midi-Bridge", "bluez_midi.server"}

    def list_audio_sources(self):
        """List running audio applications (output ports grouped by node).

        Filters out monitors, capture ports, and MIDI to show only actual
        applications producing audio.
        """
        nodes = self._list_ports("output")
        return [
            n for n in nodes
            if n["name"] not in self._SKIP_NODES
            and any(":output_" in p[1] for p in n["ports"])
        ]

    def list_audio_destinations(self):
        """List available audio input sinks/destinations."""
        nodes = self._list_ports("input")
        return [
            n for n in nodes
            if n["name"] not in self._SKIP_NODES
        ]

    def _find_node_ports(self, direction, node_name):
        """Find ports for a specific node by name."""
        for node in self._list_ports(direction):
            if node["name"] == node_name:
                return node
        return None

    def _list_ports(self, direction):
        """List ports using pw-link for a given direction.

        Groups ports into nodes. When multiple PipeWire nodes share the
        same name (e.g. two Chromium streams), they are kept separate by
        detecting non-consecutive port IDs that restart the channel
        sequence (e.g. a second FL/MONO after an FR).
        """
        flag = "-o" if direction == "output" else "-i"
        try:
            result = subprocess.run(
                ["pw-link", flag, "-I", "-v"],
                capture_output=True, text=True, timeout=5,
            )
        except subprocess.SubprocessError:
            return []

        # Collect ports in order, splitting duplicate node names into
        # separate entries when the channel pattern restarts.
        nodes = []
        seen = {}  # node_name -> index into nodes list
        for line in result.stdout.splitlines():
            line = line.rstrip()
            if not line:
                continue
            port_match = re.match(r'\s*(\d+)\s+(.+)', line)
            if not port_match:
                continue
            port_id = port_match.group(1)
            full_name = port_match.group(2)

            if ":" not in full_name:
                continue

            node_name, port_name = full_name.rsplit(":", 1)

            if node_name in ("default", ""):
                continue

            # Detect if this is a new instance of a duplicate node name
            # by checking if the channel (e.g. _FL) already exists in
            # the current group for this name.
            is_new = True
            if node_name in seen:
                idx = seen[node_name]
                existing_channels = {
                    p[1].rsplit(":", 1)[1] for p in nodes[idx]["ports"]
                }
                if port_name not in existing_channels:
                    is_new = False

            if is_new and node_name in seen:
                # Start a new group for this duplicate node name
                nodes.append({"name": node_name, "ports": []})
                seen[node_name] = len(nodes) - 1
            elif node_name not in seen:
                nodes.append({"name": node_name, "ports": []})
                seen[node_name] = len(nodes) - 1

            nodes[seen[node_name]]["ports"].append((port_id, full_name))

        return nodes

    # ------------------------------------------------------------------
    # Link management
    # ------------------------------------------------------------------

    def create_link(self, source, dest, src_name=None, dst_name=None):
        """Create a pw-link between an output port and an input port.

        source/dest can be port IDs or port names.  When IDs are used,
        pass src_name/dst_name for display purposes.
        """
        try:
            result = subprocess.run(
                ["pw-link", str(source), str(dest)],
                capture_output=True, text=True, timeout=5,
            )
            # pw-link returns 0 on success.  When the link already exists
            # it returns non-zero with "File exists" in stderr.
            linked = (
                result.returncode == 0
                or "File exists" in result.stderr
                or "already linked" in result.stderr
            )
            if linked:
                entry = {
                    "src_id": str(source),
                    "dst_id": str(dest),
                    "src_name": src_name or str(source),
                    "dst_name": dst_name or str(dest),
                }
                # Avoid duplicates by port ID pair
                if not any(
                    t["src_id"] == entry["src_id"] and t["dst_id"] == entry["dst_id"]
                    for t in self._tracked_ports
                ):
                    self._tracked_ports.append(entry)
                return True
            return False
        except subprocess.SubprocessError:
            return False

    def destroy_link(self, link):
        """Destroy a tracked link using its port IDs."""
        try:
            subprocess.run(
                ["pw-link", "-d", link["src_id"], link["dst_id"]],
                capture_output=True, text=True, timeout=5,
            )
        except subprocess.SubprocessError:
            pass
        self._tracked_ports = [
            t for t in self._tracked_ports
            if not (t["src_id"] == link["src_id"] and t["dst_id"] == link["dst_id"])
        ]

    def destroy_all_links(self):
        """Destroy only the links we created."""
        for link in list(self._tracked_ports):
            try:
                subprocess.run(
                    ["pw-link", "-d", link["src_id"], link["dst_id"]],
                    capture_output=True, text=True, timeout=5,
                )
            except subprocess.SubprocessError:
                pass
        self._tracked_ports.clear()

    def get_active_links(self):
        """Return tracked links, pruning any whose ports no longer exist."""
        self._cleanup_stale_links()
        return list(self._tracked_ports)

    def _cleanup_stale_links(self):
        """Remove tracked entries whose source ports no longer exist in PipeWire."""
        if not self._tracked_ports:
            return
        try:
            result = subprocess.run(
                ["pw-link", "-o", "-I"],
                capture_output=True, text=True, timeout=5,
            )
        except subprocess.SubprocessError:
            return

        live_ids = set()
        for line in result.stdout.splitlines():
            m = re.match(r'\s*(\d+)\s+', line)
            if m:
                live_ids.add(m.group(1))

        self._tracked_ports = [
            t for t in self._tracked_ports if t["src_id"] in live_ids
        ]

    def route_source_to_destination(self, source_node, dest_node):
        """Route all audio channels from source to destination.

        Matches FL→FL, FR→FR etc. by pairing ports in order.
        Uses port IDs to avoid ambiguity when multiple nodes share a name.
        """
        src_ports = source_node["ports"]
        dst_ports = dest_node["ports"]
        count = min(len(src_ports), len(dst_ports))
        success = True
        for i in range(count):
            # Use port IDs for linking to handle duplicate node names,
            # but track by name for display and fallback deletion.
            src_id, src_name = src_ports[i]
            dst_id, dst_name = dst_ports[i]
            if not self.create_link(src_id, dst_id, src_name, dst_name):
                success = False
        return success

    # ------------------------------------------------------------------
    # Sticky routing rules
    # ------------------------------------------------------------------

    def add_routing_rule(self, src_app_name, dest_node):
        """Add a sticky rule: all streams from src_app_name -> dest_node."""
        # Remove any existing rule for this app name
        self._routing_rules = [
            r for r in self._routing_rules if r["src_name"] != src_app_name
        ]
        self._routing_rules.append({
            "src_name": src_app_name,
            "dst_node": dest_node,
        })

    def remove_routing_rule(self, src_app_name):
        """Remove a sticky rule for an app."""
        self._routing_rules = [
            r for r in self._routing_rules if r["src_name"] != src_app_name
        ]

    def clear_routing_rules(self):
        """Remove all sticky routing rules."""
        self._routing_rules.clear()

    def get_routing_rules(self):
        """Return active routing rules."""
        return list(self._routing_rules)

    def enforce_routing_rules(self):
        """Scan for streams matching active rules and link any unlinked ones."""
        if not self._routing_rules:
            return

        # Prune stale entries first so dead port IDs don't block re-routing
        self._cleanup_stale_links()

        sources = self.list_audio_sources()
        tracked_src_ids = {t["src_id"] for t in self._tracked_ports}

        for rule in self._routing_rules:
            # Refresh destination ports (they may have new IDs too)
            dst_node = self._find_node_ports("input", rule["dst_node"]["name"])
            if not dst_node:
                continue

            # Find all source streams matching this app name
            for src_node in sources:
                if src_node["name"] != rule["src_name"]:
                    continue

                # Check if this stream's ports are already linked
                src_ids = {p[0] for p in src_node["ports"]}
                if src_ids & tracked_src_ids:
                    continue  # already routed

                # New stream found — link it
                self.route_source_to_destination(src_node, dst_node)


class SystemTrayApp:
    """Qt system tray application for PipeWire audio routing."""

    def __init__(self):
        self.app = QApplication(sys.argv)
        self.app.setQuitOnLastWindowClosed(False)

        self.pw = PipeWireManager()

        # Track user selections
        self._selected_source_name = None  # app name string
        self._selected_dest = None         # node dict

        # Create tray icon
        self.tray = QSystemTrayIcon()
        icon_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "icon.svg")
        self.tray.setIcon(QIcon(icon_path))
        self.tray.setToolTip("PipeWire Audio Router")

        # Build menu
        self.menu = QMenu()
        self.tray.setContextMenu(self.menu)

        # Rebuild menu each time it's shown so the app list is fresh
        self.menu.aboutToShow.connect(self._rebuild_menu)

        # On wlroots-based Wayland compositors (Hyprland, Sway, etc.) the
        # right-click context menu may not appear because the tray host only
        # forwards left-click / Activate.  Show the menu on any click so it
        # works everywhere.
        self.tray.activated.connect(self._on_tray_activated)

        # Poll for new streams matching active routing rules
        self._enforce_timer = QTimer()
        self._enforce_timer.timeout.connect(self.pw.enforce_routing_rules)
        self._enforce_timer.start(2000)  # every 2 seconds

        self.tray.show()

    def _on_tray_activated(self, reason):
        """Show the context menu on any click (left, middle, or right).

        Works around wlroots-based compositors (Hyprland, Sway) where
        right-click / context menu activation is not forwarded to Qt.
        """
        if reason in (
            QSystemTrayIcon.ActivationReason.Trigger,       # left click
            QSystemTrayIcon.ActivationReason.MiddleClick,
            QSystemTrayIcon.ActivationReason.Context,        # right click
        ):
            if self.menu.isVisible():
                self.menu.close()
                return
            geo = self.tray.geometry()
            if geo.isValid() and not geo.isNull():
                pos = geo.center()
            else:
                pos = QCursor.pos()
            self.menu.popup(pos)

    # ------------------------------------------------------------------
    # Menu construction
    # ------------------------------------------------------------------

    @staticmethod
    def _unique_source_names(sources):
        """Return deduplicated list of app names from source nodes."""
        seen = set()
        names = []
        for s in sources:
            if s["name"] not in seen:
                seen.add(s["name"])
                names.append(s["name"])
        return names

    @staticmethod
    def _label_nodes(nodes):
        """Add display labels, numbering duplicates (e.g. 'Chromium #1')."""
        name_count = {}
        for n in nodes:
            name_count[n["name"]] = name_count.get(n["name"], 0) + 1

        name_seq = {}
        for n in nodes:
            if name_count[n["name"]] > 1:
                seq = name_seq.get(n["name"], 0) + 1
                name_seq[n["name"]] = seq
                n["label"] = f"{n['name']} #{seq}"
            else:
                n["label"] = n["name"]
        return nodes

    def _rebuild_menu(self):
        self.menu.clear()

        # --- Route Audio submenu (sources) ---
        # Show each app name once; selecting it routes ALL its streams.
        source_menu = self.menu.addMenu("Route Audio    ")
        sources = self.pw.list_audio_sources()
        source_names = self._unique_source_names(sources)
        active_rules = {r["src_name"] for r in self.pw.get_routing_rules()}
        if source_names:
            for name in source_names:
                action = QAction(name, self.menu)
                action.setCheckable(True)
                action.setChecked(name in active_rules)
                action.triggered.connect(
                    lambda checked, n=name: self._on_source_selected(n)
                )
                source_menu.addAction(action)
        else:
            no_src = source_menu.addAction("(no audio apps running)")
            no_src.setEnabled(False)

        # --- Route To submenu (destinations) ---
        dest_menu = self.menu.addMenu("Route To    ")
        destinations = self._label_nodes(self.pw.list_audio_destinations())
        if destinations:
            for dst in destinations:
                action = QAction(dst["label"], self.menu)
                action.setCheckable(True)
                action.setChecked(
                    self._selected_dest is not None
                    and self._selected_dest["name"] == dst["name"]
                )
                action.triggered.connect(
                    lambda checked, d=dst: self._on_dest_selected(d)
                )
                dest_menu.addAction(action)
        else:
            no_dst = dest_menu.addAction("(no destinations found)")
            no_dst.setEnabled(False)

        self.menu.addSeparator()

        # --- Virtual Microphone toggle ---
        if self.pw.virtual_mic_active:
            vm_action = self.menu.addAction("Disable Virtual Microphone")
        else:
            vm_action = self.menu.addAction("Enable Virtual Microphone")
        vm_action.triggered.connect(self._on_toggle_virtual_mic)

        self.menu.addSeparator()

        # --- Active Routes ---
        # Group per-channel links into one display entry per app->dest pair
        links = self.pw.get_active_links()
        route_groups = {}  # (src_app, dst_app) -> list of link dicts
        for link in links:
            src_app = link["src_name"].rsplit(":", 1)[0]
            dst_app = link["dst_name"].rsplit(":", 1)[0]
            key = (src_app, dst_app)
            route_groups.setdefault(key, []).append(link)

        if route_groups:
            header = self.menu.addAction("Active Routes:")
            header.setEnabled(False)
            for (src_app, dst_app), group_links in route_groups.items():
                label = f"  {src_app} -> {dst_app}  [x]"
                rm_action = self.menu.addAction(label)
                rm_action.triggered.connect(
                    lambda checked, ll=group_links: self._on_remove_link_group(ll)
                )

            self.menu.addSeparator()
            stop_action = self.menu.addAction("Stop All Routes")
            stop_action.triggered.connect(self._on_stop_all)
            self.menu.addSeparator()

        # --- Quit ---
        quit_action = self.menu.addAction("Quit")
        quit_action.triggered.connect(self._on_quit)

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def _on_source_selected(self, app_name):
        self._selected_source_name = app_name
        self._try_auto_route()

    def _on_dest_selected(self, dest):
        self._selected_dest = dest
        self._try_auto_route()

    def _try_auto_route(self):
        """If both source and destination are selected, create the route."""
        if not getattr(self, '_selected_source_name', None) or self._selected_dest is None:
            return

        # Auto-create virtual sink if the destination is the virtual sink
        if (
            self.VIRTUAL_SINK_NAME_MATCH(self._selected_dest["name"])
            and not self.pw.virtual_sink_active
        ):
            self.pw.create_virtual_sink()
            # Refresh the destination port list
            for d in self.pw.list_audio_destinations():
                if self.VIRTUAL_SINK_NAME_MATCH(d["name"]):
                    self._selected_dest = d
                    break

        # Route ALL current streams matching this app name
        sources = self.pw.list_audio_sources()
        routed = False
        for src_node in sources:
            if src_node["name"] == self._selected_source_name:
                self.pw.route_source_to_destination(src_node, self._selected_dest)
                routed = True

        # Register a sticky rule so new streams get auto-routed
        self.pw.add_routing_rule(
            self._selected_source_name, self._selected_dest
        )

        if routed:
            self.tray.showMessage(
                "Audio Routed",
                f"{self._selected_source_name} -> {self._selected_dest['name']}",
                QSystemTrayIcon.MessageIcon.Information,
                2000,
            )

    @staticmethod
    def VIRTUAL_SINK_NAME_MATCH(name):
        return PipeWireManager.VIRTUAL_SINK_NAME.lower() in name.lower()

    def _on_remove_link_group(self, link_group):
        """Remove all channel links for a route and its routing rule."""
        if link_group:
            src_app = link_group[0]["src_name"].rsplit(":", 1)[0]
            self.pw.remove_routing_rule(src_app)
        for link in link_group:
            self.pw.destroy_link(link)

    def _on_toggle_virtual_mic(self):
        if self.pw.virtual_mic_active:
            self.pw.destroy_virtual_mic()
        else:
            self.pw.create_virtual_mic()

    def _on_stop_all(self):
        self.pw.clear_routing_rules()
        self.pw.destroy_all_links()
        self._selected_source_name = None
        self._selected_dest = None

    def _on_quit(self):
        self._enforce_timer.stop()
        self.pw.clear_routing_rules()
        self.pw.destroy_all_links()
        self.pw.destroy_virtual_sink()
        self.pw.destroy_virtual_mic()
        self.app.quit()

    # ------------------------------------------------------------------
    # Run
    # ------------------------------------------------------------------

    def run(self):
        return self.app.exec()


def main():
    # Verify PipeWire tools are available
    for tool in ("pw-cli", "pw-link", "pactl"):
        try:
            subprocess.run(
                [tool, "--version"],
                capture_output=True, timeout=3,
            )
        except FileNotFoundError:
            print(f"Error: '{tool}' not found. Install PipeWire utilities.")
            sys.exit(1)
        except subprocess.SubprocessError:
            pass  # --version may not be supported but the binary exists

    app = SystemTrayApp()
    sys.exit(app.run())


if __name__ == "__main__":
    main()

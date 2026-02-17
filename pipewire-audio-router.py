#!/usr/bin/env python3
"""PipeWire Audio Router - System tray app for routing application audio."""

import json
import os
import re
import subprocess
import sys

from PyQt6.QtCore import QTimer
from PyQt6.QtGui import QAction, QIcon
from PyQt6.QtWidgets import QApplication, QMenu, QSystemTrayIcon


class PipeWireManager:
    """Manages PipeWire virtual sinks and audio routing."""

    VIRTUAL_SINK_NAME = "ScreenShareAudio"

    def __init__(self):
        self._virtual_sink_id = None
        self._active_links = []  # list of (output_port, input_port) tuples
        # Check if we already have a virtual sink from a previous session
        self._detect_existing_virtual_sink()

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
    # Listing sources and destinations
    # ------------------------------------------------------------------

    def list_audio_sources(self):
        """List running audio applications (output ports grouped by node).

        Returns a list of dicts: {name, node_id, ports: [(port_id, port_name)]}
        """
        return self._list_ports("output")

    def list_audio_destinations(self):
        """List available audio input sinks/destinations.

        Returns a list of dicts: {name, node_id, ports: [(port_id, port_name)]}
        """
        return self._list_ports("input")

    def _list_ports(self, direction):
        """List ports using pw-link for a given direction."""
        flag = "-o" if direction == "output" else "-i"
        try:
            result = subprocess.run(
                ["pw-link", flag, "-I", "-v"],
                capture_output=True, text=True, timeout=5,
            )
        except subprocess.SubprocessError:
            return []

        nodes = {}
        for line in result.stdout.splitlines():
            line = line.rstrip()
            if not line:
                continue
            # Lines look like:
            #   <port_id> <node_name>:<port_channel>
            # or with verbose mode:
            #   <port_id> <node_name>:output_FL   (or :playback_FL, :monitor_FL etc.)
            port_match = re.match(r'\s*(\d+)\s+(.+)', line)
            if not port_match:
                continue
            port_id = port_match.group(1)
            full_name = port_match.group(2)

            if ":" not in full_name:
                continue

            node_name, port_name = full_name.rsplit(":", 1)

            # Skip PipeWire internal nodes we don't care about
            if node_name in ("default", ""):
                continue

            if node_name not in nodes:
                nodes[node_name] = {
                    "name": node_name,
                    "ports": [],
                }
            nodes[node_name]["ports"].append((port_id, full_name))

        return list(nodes.values())

    # ------------------------------------------------------------------
    # Link management
    # ------------------------------------------------------------------

    def create_link(self, source_port_name, dest_port_name):
        """Create a pw-link between an output port and an input port."""
        try:
            result = subprocess.run(
                ["pw-link", source_port_name, dest_port_name],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0 or "already linked" in result.stderr:
                link = (source_port_name, dest_port_name)
                if link not in self._active_links:
                    self._active_links.append(link)
                return True
            return False
        except subprocess.SubprocessError:
            return False

    def destroy_link(self, source_port_name, dest_port_name):
        """Destroy a pw-link between two ports."""
        try:
            subprocess.run(
                ["pw-link", "-d", source_port_name, dest_port_name],
                capture_output=True, text=True, timeout=5,
            )
        except subprocess.SubprocessError:
            pass
        link = (source_port_name, dest_port_name)
        if link in self._active_links:
            self._active_links.remove(link)

    def destroy_all_links(self):
        """Destroy all links we created."""
        for src, dst in list(self._active_links):
            self.destroy_link(src, dst)
        self._active_links.clear()

    def get_active_links(self):
        """Return a copy of the active links list."""
        return list(self._active_links)

    def route_source_to_destination(self, source_node, dest_node):
        """Route all audio channels from source to destination.

        Matches FL→FL, FR→FR etc. by pairing ports in order.
        """
        src_ports = source_node["ports"]
        dst_ports = dest_node["ports"]
        count = min(len(src_ports), len(dst_ports))
        success = True
        for i in range(count):
            if not self.create_link(src_ports[i][1], dst_ports[i][1]):
                success = False
        return success


class SystemTrayApp:
    """Qt system tray application for PipeWire audio routing."""

    def __init__(self):
        self.app = QApplication(sys.argv)
        self.app.setQuitOnLastWindowClosed(False)

        self.pw = PipeWireManager()

        # Track user selections
        self._selected_source = None  # node dict
        self._selected_dest = None    # node dict

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

        self.tray.show()

    # ------------------------------------------------------------------
    # Menu construction
    # ------------------------------------------------------------------

    def _rebuild_menu(self):
        self.menu.clear()

        # --- Route Audio submenu (sources) ---
        source_menu = self.menu.addMenu("Route Audio    ")
        sources = self.pw.list_audio_sources()
        if sources:
            for src in sources:
                action = QAction(src["name"], self.menu)
                action.setCheckable(True)
                action.setChecked(
                    self._selected_source is not None
                    and self._selected_source["name"] == src["name"]
                )
                action.triggered.connect(
                    lambda checked, s=src: self._on_source_selected(s)
                )
                source_menu.addAction(action)
        else:
            no_src = source_menu.addAction("(no audio apps running)")
            no_src.setEnabled(False)

        # --- Route To submenu (destinations) ---
        dest_menu = self.menu.addMenu("Route To    ")
        destinations = self.pw.list_audio_destinations()
        if destinations:
            for dst in destinations:
                action = QAction(dst["name"], self.menu)
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

        # --- Active Routes ---
        links = self.pw.get_active_links()
        if links:
            header = self.menu.addAction("Active Routes:")
            header.setEnabled(False)
            for src_port, dst_port in links:
                # Shorten to node names for display
                src_short = src_port.rsplit(":", 1)[0] if ":" in src_port else src_port
                dst_short = dst_port.rsplit(":", 1)[0] if ":" in dst_port else dst_port
                label = f"  {src_short} -> {dst_short}"
                rm_action = self.menu.addAction(label + "  [x]")
                rm_action.triggered.connect(
                    lambda checked, s=src_port, d=dst_port: self._on_remove_link(s, d)
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

    def _on_source_selected(self, source):
        self._selected_source = source
        self._try_auto_route()

    def _on_dest_selected(self, dest):
        self._selected_dest = dest
        self._try_auto_route()

    def _try_auto_route(self):
        """If both source and destination are selected, create the route."""
        if self._selected_source is None or self._selected_dest is None:
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

        # Refresh source ports in case they changed
        for s in self.pw.list_audio_sources():
            if s["name"] == self._selected_source["name"]:
                self._selected_source = s
                break

        self.pw.route_source_to_destination(
            self._selected_source, self._selected_dest
        )

        self.tray.showMessage(
            "Audio Routed",
            f"{self._selected_source['name']} -> {self._selected_dest['name']}",
            QSystemTrayIcon.MessageIcon.Information,
            2000,
        )

    @staticmethod
    def VIRTUAL_SINK_NAME_MATCH(name):
        return PipeWireManager.VIRTUAL_SINK_NAME.lower() in name.lower()

    def _on_remove_link(self, src_port, dst_port):
        self.pw.destroy_link(src_port, dst_port)

    def _on_stop_all(self):
        self.pw.destroy_all_links()
        self._selected_source = None
        self._selected_dest = None

    def _on_quit(self):
        self.pw.destroy_all_links()
        self.pw.destroy_virtual_sink()
        self.app.quit()

    # ------------------------------------------------------------------
    # Run
    # ------------------------------------------------------------------

    def run(self):
        return self.app.exec()


def main():
    # Verify PipeWire tools are available
    for tool in ("pw-cli", "pw-link"):
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

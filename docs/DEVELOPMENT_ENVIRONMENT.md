# Development Environment

## Platform

1. **Ubuntu version:** 24.04.4 LTS
2. **System Python version:** 3.12.3 (`/usr/bin/python3`) — verified, not assumed
3. **ROS2 distribution:** Jazzy (`/opt/ros/jazzy`), whose `rclpy` and message
   packages are built against this same system Python 3.12

## Project environment

4. **Project environment path:** `/home/mila/teleop_visualization_jetson/.venv`

5. **Why `--system-site-packages` is used:** ROS2 Jazzy and the
   Septentrio/Xsens ROS2 message packages are installed into the system
   Python's `site-packages` (under `/opt/ros/jazzy/lib/python3.12/...`), not
   available via PyPI. A normal isolated venv would not be able to `import
   rclpy` or any ROS2 message package. `--system-site-packages` lets the venv
   fall back to the system site-packages for these, while still giving the
   project its own `pip`/installed-packages directory for project-specific
   dependencies.

   The venv was created with the **same system Python** that ROS2 Jazzy uses
   (3.12.3) — this was verified, not assumed, before creating the environment.
   Creating the venv required installing the `python3.12-venv` system package
   first (`ensurepip` is not bundled by default on this Ubuntu image); this was
   an explicit, approved, one-time system package install, not something this
   project manages going forward.

6. **Activation command:**
   ```bash
   cd /home/mila/teleop_visualization_jetson
   source .venv/bin/activate
   ```

7. **ROS2 sourcing command:**
   ```bash
   source /opt/ros/jazzy/setup.bash
   ```

8. **rclpy verification** (run after both of the above):
   ```bash
   python -c "import rclpy; print('rclpy OK:', rclpy.__file__)"
   ```
   Confirmed working: `rclpy OK: /opt/ros/jazzy/lib/python3.12/site-packages/rclpy/__init__.py`

   Also confirmed importable from inside the venv: `sensor_msgs.msg`,
   `septentrio_gnss_driver.msg`.

## Dependency-install policy

9. Project Python dependencies are installed **only** inside the activated
   `.venv`, and only when project-owned code actually needs them:
   ```bash
   source .venv/bin/activate
   python -m pip install <package>
   ```
   Then add the exact requirement to `requirements.txt` intentionally — do not
   add packages speculatively "for later."

10. **GStreamer and NVIDIA/L4T components remain system-managed.** `ROS2`,
    `GStreamer` (`gst-launch-1.0`, `nvv4l2h264enc`, `nvvidconv`, etc.), the
    NVIDIA L4T BSP, and CUDA are installed and managed at the OS/apt level, not
    through pip, and must never be reinstalled or shadowed inside `.venv`. The
    project's Python code calls into these as external system binaries/
    libraries (e.g. via `gst-launch-1.0` subprocess calls or GStreamer Python
    bindings that themselves come from system packages, not pip).

## Recommended normal development startup

```bash
cd /home/mila/teleop_visualization_jetson
source .venv/bin/activate
source /opt/ros/jazzy/setup.bash
```

Then run project commands.

## `.venv/` is never committed

`.venv/` is listed in `.gitignore`. Only `requirements.txt` and this document
are committed — anyone setting up the project recreates the environment with:

```bash
cd /home/mila/teleop_visualization_jetson
python3 -m venv --system-site-packages .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

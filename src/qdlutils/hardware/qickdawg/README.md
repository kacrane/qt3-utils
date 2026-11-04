# QICK-DAWG on the VQLM

The VQLM laser gate is a PMOD pin on a QICK-DAWG board (sandialabs/qick-dawg on an
RFSoC running PYNQ). The lab PC talks to the board over Pyro4:

* on the board, a **Pyro4 name server** (port 8888) and a **QICK daemon** that loads the
  firmware and registers the `QickSoc` as `myqick`;
* on the PC, `qickdawg.start_client('<board ip>')`, then e.g. `qickdawg.laser_on(cfg)`.

Upstream runs the two board pieces from `run_server/name_server.ipynb` and
`run_server/qick_daemon.ipynb`.

## Why it drops after a few days

1. **The servers live in Jupyter kernels.** If the board reboots, loses power, or Jupyter
   restarts, nothing starts them again. You have to open both notebooks by hand.
2. **The server runs out of connection threads.** Pyro4's server keeps one thread per
   client connection, 40 at most. Every notebook kernel or app that connected and then
   crashed, was restarted, went to sleep, or lost the link keeps its thread. Linux only
   notices a dead peer after more than 2 hours, if ever. Once 40 have piled up, every new
   connection is refused with `no free workers, increase server threadpool size`.
   This was reproduced with the notebook's exact settings.
3. **Clients never reconnect.** After any network blip, a notebook's `qd.soc` proxy is dead
   and stays dead until the notebook is re-run.
4. **The PC side of the link.** If the PC reaches the board over the USB cable
   (192.168.3.1 is PYNQ's USB-network address), Windows can power down that USB network
   adapter. That breaks every open connection. Turn this off in Device Manager (step 4 below).

## Fix

| Piece | What it does |
|---|---|
| `board/qick_server.py` | One script that runs the name server and the QICK daemon. `--install` makes it a systemd service, so it starts at boot and restarts within 10 s if it crashes. The thread pool is 200, and TCP keepalive drops dead clients after about 2 min. A watchdog restarts everything if the board stops answering for 90 s. Same port and name as the notebooks, so `qd.start_client(...)` works unchanged. |
| `client.py` | `QickDawgClient`: connects like `start_client`, reconnects and retries once if a call fails, and has a 10 s timeout instead of hanging. Also a command-line check. |
| `applications/qdllaser` | Small laser ON/OFF window, also launched from qdlhome. It checks the board every 30 s, reconnects by itself, and shows "unknown" if the board server restarted, because a restart can reset the pin. |

## Bring-up (one time)

All `ssh`/`scp` commands run from the VQLM PC (PowerShell). Replace `192.168.3.1` if your
board has a different address. The board's default password is `xilinx`.

1. Find the firmware path your `qick_daemon.ipynb` uses (its `qick_path = ...` line). Check it exists:

       ssh xilinx@192.168.3.1 "ls /home/xilinx/jupyter_notebooks/qick-dawg/firmware/photon_counting/"

2. Stop the notebooks: in the board's Jupyter page, *Running* tab, shut down the
   `name_server` and `qick_daemon` kernels.

3. Copy the script and install the service (use your bitfile path):

       scp src\qdlutils\hardware\qickdawg\board\qick_server.py xilinx@192.168.3.1:/home/xilinx/
       ssh -t xilinx@192.168.3.1 "sudo python3 /home/xilinx/qick_server.py --install --host 192.168.3.1 --bitfile /home/xilinx/jupyter_notebooks/qick-dawg/firmware/photon_counting/qick_4x2.bit"

   Watch it start (Ctrl+C to stop watching):

       ssh -t xilinx@192.168.3.1 "sudo journalctl -u qick-server -f"

4. If the board is on the USB cable: Device Manager > Network adapters > the
   "USB Ethernet/RNDIS Gadget" (or similar) > Properties > Power Management > untick
   "Allow the computer to turn off this device to save power". Also set
   Power Options > USB selective suspend to Disabled.

5. Test from the PC:

       python -m qdlutils.hardware.qickdawg.client --host 192.168.3.1
       python -m qdlutils.hardware.qickdawg.client --host 192.168.3.1 --laser on
       python -m qdlutils.hardware.qickdawg.client --host 192.168.3.1 --laser off

6. Reboot test: `ssh -t xilinx@192.168.3.1 "sudo reboot"`, wait about 2 minutes, and run
   step 5 again without touching any notebook.

Undo at any time with
`ssh -t xilinx@192.168.3.1 "sudo python3 /home/xilinx/qick_server/qick_server.py --uninstall"`,
then use the notebooks as before.

## If it still drops

Run `python -m qdlutils.hardware.qickdawg.client --host 192.168.3.1` when it happens. The
first failing step tells you where the problem is:

* **1 fails (port closed):** the board is off, the link is down, or the service died. Run
  `ping 192.168.3.1`, then `sudo journalctl -u qick-server -n 100` on the board.
* **2 or 3 fails:** the service is up but QICK isn't answering. The journal shows why, and
  the watchdog will restart it within about 2 minutes.

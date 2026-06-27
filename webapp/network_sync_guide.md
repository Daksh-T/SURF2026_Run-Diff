# Run·Diff — Network Sync & Class Server Guide

How students get their assignment and how their attempts get back to you. There are two
mechanisms; you can use either or both, and **the assignment file is always a working backup**.

| Mechanism | How the assignment travels | How attempts come back | Needs network? |
|-----------|----------------------------|------------------------|----------------|
| **File sync** (default) | You hand out a `.json` assignment file | Students export an attempts file; you import it | No |
| **Network sync** | Students *connect* to your machine over the LAN (or import a file that carries your address) | Pushed live to your machine as students work | Yes — same Wi-Fi/LAN |

Network sync layers on top of file sync: it does **not** replace it. Every exported assignment
file still contains your class-server address, so a student who can't reach the network can fall
back to the file and you can fall back to importing their attempts file.

---

## Concepts

- **Class server** — one machine (yours) running Run·Diff with *Host on this network* turned on.
  It serves the assignment to students and ingests their attempts.
- **Class code** — the class passphrase (e.g. `maple-river-stone`) for open/roster classes, or a
  student's personal passcode for passcode classes. It is the credential students use both to
  *join* and to *fetch the assignment over the network*.
- **Class server address** — the URL of your machine on the LAN, e.g. `http://192.168.1.5:8077`.
- **Assignment file** — a sealed `.json` containing the class, its published sets, and your
  class-server address. Always usable offline.

Grading is **local** on each student's device (the bundle ships with baked gold *results*, never
gold SQL), so students can keep working even if the network drops mid-session — their attempts
queue locally and sync when they reconnect, or export to a file.

---

## Instructor setup

### 1. Publish your set(s) and create a class
On **Author → Sets**, publish a set. On **Author → Classes**, create a class assigning it and
choose a mode (open / roster / passcode). Note the class code shown on the class card.

### 2. Turn on hosting
At the top of **Author → Classes** is the sync panel:

- Click **Host on this network**. Run·Diff auto-detects this machine's LAN address and sets it as
  the class-server address. (If several network interfaces are detected, pick the right one from
  the dropdown first — usually your Wi-Fi address.)
- The panel then shows the **address** and a **QR code** of it.
- The first time, macOS may ask *"Do you want the application to accept incoming network
  connections?"* — choose **Allow**. (This is required for students to reach you.)

To use a specific address instead, click **Enter URL manually** (e.g. a fixed IP or a hostname).
To stop hosting, click **Turn off** — the class reverts to file sync.

### 3. Share with students
Give students **two things**: the **class-server address** (read it out, or let them scan the QR)
and their **class code / passcode**. That's all they need to connect.

Prefer files? **Export the assignment file** from the class card and distribute it — it carries
your address, so students who import it are also set up for live sync automatically.

### 4. Watch attempts arrive
**Author → Insights** updates as students push attempts. If a student couldn't reach the network,
have them **Export attempts** and use **Import attempts** on the class card.

---

## Student setup

On the **Practice** sign-in screen you always enter your **class code / passcode** and **your
name**. Then choose how to connect:

1. **Class server address** (optional field) — enter the address your instructor gave you (or scan
   the QR). The button becomes **Connect**: the assignment downloads over the LAN and attempts
   sync live. Leave it blank and the button is **Join**, which works when the class already lives
   on this device.
2. **…or load an assignment file** — if you were given a `.json` file, enter your name and choose
   the file. If the file carries a server address, live sync turns on automatically; otherwise use
   **Export attempts** to hand work back as a file.

You must be on the **same Wi-Fi/LAN** as the class server to connect or sync live. If you can't,
the file path always works.

---

## Requirements & networking notes

- **Same network.** Students and the class server must share a LAN/Wi-Fi and be able to reach each
  other. Many **campus/guest Wi-Fi networks isolate clients** (so devices can't see each other) or
  block the needed ports — in that case use the **file** workflow, or a network where you control
  client isolation (a dedicated router / hotspot).
- **Port 8077.** The class server listens on `8077`. If a firewall blocks it, students get
  "could not reach class server."
- **Address can change.** A laptop's LAN IP can change between sessions (DHCP). If students can't
  connect, re-open the sync panel and re-share the current address (the QR always reflects it).
- **Find your address manually** (sanity check): macOS `ipconfig getifaddr en0`, or
  System Settings → Wi-Fi → Details.

---

## Security model

- The class server binds to the LAN while Run·Diff is running. **Instructor/authoring endpoints
  are local-only**: they answer only requests coming from this machine (loopback), so binding to
  the LAN to host a class never exposes authoring, publishing, or class management to other
  devices — even before you set an author password. The optional author password is a second
  layer on top of that (and is what gates the local UI on a shared machine). Student endpoints
  (join, fetch assignment, sync attempts) are reachable on the LAN by design.
  - *Self-hosting note:* if you intentionally run the backend headless and drive the Author UI
    from another machine's browser over the LAN, set `RUNDIFF_ALLOW_REMOTE_ADMIN=1` to allow it
    (then a password is strongly recommended). This is off by default.
- **The class code is the credential.** Anyone on the LAN who knows a class code can fetch that
  class's assignment — exactly like handing out the file. Assignments are **student-safe**: they
  contain baked gold *results*, never gold SQL, and are sealed on the wire.
- Attempt sync is authenticated by the class passphrase; attempts are de-duplicated on ingest.
- Turning hosting **off** stops advertising the address; new exports go back to file-only.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---------|--------------|-----|
| "No LAN address detected" | Not on a network, or only loopback | Join a Wi-Fi/LAN, reopen the panel |
| Student: "could not reach class server" | Different network, firewall, or wrong/stale address | Same Wi-Fi; Allow the firewall prompt; re-share the current address |
| Student: "no class on that server matches your code" | Wrong code, or set not published | Re-check the code; ensure the set is published and assigned |
| Connects but attempts don't appear | Class archived/closed/scheduled | Check the class status on the card |
| Campus Wi-Fi blocks everything | Client isolation | Use the file workflow, or a hotspot/router you control |

When in doubt, the **file workflow always works**: export the assignment to students, import their
attempts files back.

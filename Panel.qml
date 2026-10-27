import QtQuick
import QtQuick.Controls
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Ui

// Process Pulse: grouped processes in the Omarchy bar.
// Bar: bug glyph, red count when something is in trouble.
// Panel: app groups with live CPU/RAM, trouble badges, search,
// one-press TERM/KILL per group or per process.
// Safety: kill.py re-validates every PID against the last snapshot
// (command, start time, owner) before signalling.
Panel {
  id: root
  moduleName: "meviusisback.kill-procs"
  ipcTarget: "meviusisback.kill-procs"
  manageIpc: false

  implicitWidth: Math.max(barRow.implicitWidth, 1)
  implicitHeight: Math.max(barRow.implicitHeight, 1)

  readonly property color foreground: bar ? bar.foreground : Color.foreground
  readonly property color dim: Color.muted
  readonly property color urgent: bar ? bar.urgent : Color.urgent
  readonly property color accent: Color.accent
  readonly property string fontFamily: bar ? bar.fontFamily : Style.font.family

  // ---------------------------------------------------------------- settings
  readonly property int refreshIntervalSec: Math.min(30, Math.max(1, Number(setting("refreshIntervalSec", 2)) || 2))
  readonly property int runawaySamples: Math.min(30, Math.max(2, Number(setting("runawaySamples", 5)) || 5))
  readonly property string sortMode: String(setting("sort", "severity"))
  readonly property bool showTotals: String(setting("showTotals", true)) !== "false"

  // --------------------------------------------------------------------- data
  property var groups: []
  property var totals: ({})
  property var issues: ({})
  property bool loading: true
  property bool busy: false
  property string errorText: ""

  property string searchText: ""
  property string mode: "all"
  property string expanded: ""
  property int cursor: -1
  property var confirm: null
  property string toast: ""

  readonly property int problemCount: Number(issues.total || 0)

  function scriptPath(name) {
    return Qt.resolvedUrl(name).toString().replace(/^file:\/\//, "")
  }

  // -------------------------------------------------------------- visibility
  readonly property var visibleGroups: (function () {
    var q = root.searchText.trim().toLowerCase()
    var out = []
    for (var i = 0; i < root.groups.length; i++) {
      var g = root.groups[i]
      if (root.mode === "issues" && g.severity === "ok") continue
      if (q !== "") {
        var hit = (g.label || "").toLowerCase().indexOf(q) !== -1
        if (!hit && g.procs) {
          for (var j = 0; j < g.procs.length; j++) {
            var p = g.procs[j]
            if (String(p.pid).indexOf(q) !== -1
                || (p.label || "").toLowerCase().indexOf(q) !== -1
                || (p.preview || "").toLowerCase().indexOf(q) !== -1) { hit = true; break }
          }
        }
        if (!hit) continue
      }
      out.push(g)
    }
    var mode = root.mode
    var sortMode = root.sortMode
    out.sort(function (a, b) {
      if (mode === "cpu") return b.cpu - a.cpu
      if (mode === "mem") return b.mem - a.mem
      var rank = { critical: 0, warning: 1, ok: 2 }
      if (sortMode === "cpu") return b.cpu - a.cpu
      if (sortMode === "mem") return b.mem - a.mem
      if (sortMode === "name") return String(a.label).toLowerCase() < String(b.label).toLowerCase() ? -1 : 1
      var d = (rank[a.severity] || 2) - (rank[b.severity] || 2)
      if (d !== 0) return d
      d = b.cpu - a.cpu
      return d !== 0 ? d : String(a.label).toLowerCase() < String(b.label).toLowerCase() ? -1 : 1
    })
    return out.slice(0, 60)
  })()

  // ------------------------------------------------------------------ helpers
  function sevColor(sev) {
    if (sev === "critical") return root.urgent
    if (sev === "warning") return Qt.rgba(root.foreground.r, (root.foreground.g + root.urgent.g) / 2, root.foreground.b * 0.5, 1)
    return root.dim
  }
  function fmtCpu(v) { return v >= 100 ? String(Math.round(v)) : (Math.round(v * 10) / 10).toString() + "%" }
  function fmtMem(m) { return m >= 1024 ? (Math.round(m / 102.4) / 10).toFixed(1) + " GiB" : Math.round(m) + " MiB" }
  function fmtAge(s) {
    if (s >= 86400) return Math.round(s / 86400) + "d"
    if (s >= 3600) return Math.round(s / 3600) + "h"
    if (s >= 60) return Math.round(s / 60) + "m"
    return Math.round(s) + "s"
  }
  function stateGlyph(code) {
    if (code === "Z") return "Z"
    if (code === "D") return "D"
    if (code === "R") return "R"
    if (code === "T" || code === "t") return "S"
    return "s"
  }

  // ------------------------------------------------------------------ refresh
  function parse(text) {
    try {
      var d = JSON.parse(text || "{}")
      if (d.error) { root.errorText = String(d.error); root.loading = false; return }
      root.groups = d.groups || []
      root.totals = d.totals || {}
      root.issues = d.issues || {}
      root.loading = false
      root.errorText = ""
      if (root.cursor >= root.visibleGroups.length) root.cursor = root.visibleGroups.length - 1
    } catch (e) {
      root.errorText = "parse-error"
      root.loading = false
    }
  }

  function refresh() {
    if (probeProc.running) return
    probeProc.command = ["/usr/bin/python3", scriptPath("probe.py"), "snapshot",
      "--interval", String(root.refreshIntervalSec),
      "--runaway-samples", String(root.runawaySamples)]
    probeProc.running = true
  }

  Process {
    id: probeProc
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: root.parse(text)
    }
    stderr: StdioCollector {
      waitForEnd: true
      onStreamFinished: if ((text || "").trim() !== "") console.warn("kill-procs probe:", text.trim().slice(0, 300))
    }
    onExited: function (code) {
      root.loading = false
      if (code !== 0 && root.errorText === "") root.errorText = "probe-exit-" + code
    }
  }

  Timer {
    interval: Math.max(1, root.refreshIntervalSec) * 1000
    running: root.opened
    repeat: true
    triggeredOnStart: true
    onTriggered: root.refresh()
  }

  // --------------------------------------------------------------------- kill
  function askKillGroup(g) {
    if (root.busy || !g) return
    root.confirm = { label: g.label, pids: g.pids, n: g.pids.length, sig: "term" }
  }
  function askKillProc(g, p) {
    if (root.busy || !g || !p) return
    root.confirm = { label: g.label + " · PID " + p.pid, pids: [p.pid], n: 1, sig: "term" }
  }
  function askForceGroup(g) {
    if (root.busy || !g) return
    root.confirm = { label: g.label, pids: g.pids, n: g.pids.length, sig: "kill" }
  }

  function runConfirm() {
    var c = root.confirm
    root.confirm = null
    if (!c || root.busy) return
    var args = ["/usr/bin/python3", scriptPath("kill.py"), "--sig", c.sig]
    var pids = (c.pids || []).slice(0, 100)
    for (var i = 0; i < pids.length; i++) {
      args.push("--pid")
      args.push(String(pids[i]))
    }
    root.busy = true
    killProc.command = args
    killProc.running = true
  }

  function describeKill(d) {
    if (!d) return "kill: no output"
    if (d.ok === false) return "kill failed: " + (d.error || "unknown")
    var parts = []
    if (d.term && d.term.length) parts.push(d.term.length + " stopped")
    if (d.killed && d.killed.length) parts.push(d.killed.length + " force-killed")
    var ref = d.refused ? Object.keys(d.refused) : []
    if (ref.length) parts.push(ref.length + " refused (" + d.refused[ref[0]] + ")")
    return parts.join(" · ") || "nothing to do"
  }

  Process {
    id: killProc
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: {
        root.busy = false
        try {
          var d = JSON.parse(text || "{}")
          root.toast = root.describeKill(d)
        } catch (e) {
          root.toast = "kill: parse error"
        }
        toastTimer.restart()
        refreshAgain.restart()
      }
    }
    stderr: StdioCollector {
      waitForEnd: true
      onStreamFinished: if ((text || "").trim() !== "") console.warn("kill-procs kill:", text.trim().slice(0, 300))
    }
    onExited: root.busy = false
  }

  Timer {
    id: refreshAgain
    interval: 500
    onTriggered: root.refresh()
  }
  Timer {
    id: toastTimer
    interval: 5000
    onTriggered: root.toast = ""
  }

  // ---------------------------------------------------------------------- ipc
  IpcHandler {
    enabled: !root.manageIpc && root.ipcTarget !== ""
    target: root.ipcTarget
    function open(): void { root.open() }
    function close(): void { root.close() }
    function show(): void { root.open() }
    function hide(): void { root.close() }
    function toggle(): void { root.toggle() }
    function refresh(): string { root.refresh(); return "ok" }
    function search(q: string): string {
      root.searchText = String(q || "")
      return String(root.visibleGroups.length)
    }
    function kill(pid: int, sig: string): string {
      var s = (sig === "kill") ? "kill" : "term"
      root.confirm = { label: "PID " + pid, pids: [pid], n: 1, sig: s }
      root.runConfirm()
      return "kill-" + s + "-pid-" + pid
    }
  }

  // ---------------------------------------------------------------------- bar
  Item {
    id: barRow
    // Same box as the kit's WidgetButton: 8.5 px each side, 6 px top/bottom,
    // so the icon is not glued to the neighbouring widget.
    implicitWidth: barGlyphRow.implicitWidth + 17
    implicitHeight: barGlyphRow.implicitHeight + 2 * Style.space(6)

    Row {
      id: barGlyphRow
      spacing: Style.space(5)
      anchors.centerIn: parent

      Text {
        text: "\uf188"
        textFormat: Text.PlainText
        color: root.problemCount > 0 ? root.urgent : root.dim
        font.family: root.fontFamily
        font.pixelSize: Style.font.icon
        anchors.verticalCenter: parent.verticalCenter
      }
      Text {
        visible: root.problemCount > 0
        text: String(root.problemCount)
        textFormat: Text.PlainText
        color: root.urgent
        font.family: root.fontFamily
        font.pixelSize: Style.font.body
        font.bold: true
        anchors.verticalCenter: parent.verticalCenter
      }
    }

    MouseArea {
      anchors.fill: parent
      acceptedButtons: Qt.LeftButton | Qt.RightButton
      onClicked: function (mouse) {
        if (mouse.button === Qt.RightButton) root.refresh()
        else root.toggle()
      }
    }
  }

  // -------------------------------------------------------------------- panel
  Item {
    id: panelAnchor
    anchors.right: parent.right
    width: 1
    height: parent.height
  }

  KeyboardPanel {
    id: panel
    anchorItem: panelAnchor
    owner: root
    bar: root.bar
    open: root.opened
    focusTarget: keyCatcher
    contentWidth: Style.space(540)
    contentHeight: panel.fittedContentHeight(contentCol.implicitHeight + Style.space(70), Style.space(760))

    PanelKeyCatcher {
      id: keyCatcher
      anchors.fill: parent
      blocked: searchField.activeFocus

      onCloseRequested: {
        if (root.confirm !== null) { root.confirm = null; return }
        if (root.searchText !== "") { root.searchText = ""; searchField.text = ""; return }
        root.close()
      }
      onMoveRequested: function (dx, dy) {
        var n = root.visibleGroups.length
        if (n === 0) return
        var c = root.cursor + (dy > 0 ? 1 : dy < 0 ? -1 : 0) + (dx > 0 ? 1 : dx < 0 ? -1 : 0)
        root.cursor = Math.max(0, Math.min(n - 1, c))
      }
      onActivateRequested: {
        var g = root.visibleGroups[root.cursor]
        if (g) root.expanded = (root.expanded === g.id) ? "" : g.id
      }
      onReturnRequested: onActivateRequested()
      onDeleteRequested: {
        var g = root.visibleGroups[root.cursor]
        if (g) root.askKillGroup(g)
      }
      onTextKey: function (t) {
        if (root.confirm !== null) return
        if (root.searchText === "" && !searchField.activeFocus) {
          if (t === "/") { searchField.forceActiveFocus(); return }
          if (t === "r") { root.refresh(); return }
          if (t === "d") {
            var g = root.visibleGroups[root.cursor >= 0 ? root.cursor : 0]
            if (g) root.askKillGroup(g)
            return
          }
          if (t === "j" || t === "k") {
            var n = root.visibleGroups.length
            if (n === 0) return
            var c = root.cursor < 0 ? 0 : root.cursor + (t === "j" ? 1 : -1)
            root.cursor = Math.max(0, Math.min(n - 1, c))
            return
          }
        }
        root.searchText += t
        searchField.text = root.searchText
      }

      Flickable {
        id: panelFlick
        anchors.fill: parent
        contentWidth: width
        contentHeight: contentCol.implicitHeight
        clip: true
        boundsBehavior: Flickable.StopAtBounds
        interactive: contentHeight > height
        ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }

        Column {
          id: contentCol
          width: panelFlick.width
          spacing: Style.space(14)

          // ---------------------------------------------------------- hero
          Item {
            width: parent.width
            implicitHeight: Math.max(heroCount.implicitHeight, heroLabels.implicitHeight)

            Text {
              id: heroCount
              textFormat: Text.PlainText
              text: root.loading && root.groups.length === 0 ? "…" : String(root.problemCount)
              color: root.problemCount > 0 ? root.urgent : root.foreground
              font.family: root.fontFamily
              font.pixelSize: Style.font.display
              font.bold: true
              anchors.left: parent.left
              anchors.verticalCenter: parent.verticalCenter
            }

            Column {
              id: heroLabels
              anchors.left: heroCount.right
              anchors.leftMargin: Style.space(14)
              anchors.verticalCenter: parent.verticalCenter
              spacing: Style.space(3)

              Text {
                textFormat: Text.PlainText
                text: root.problemCount > 0 ? (root.problemCount === 1 ? "GROUP IN TROUBLE" : "GROUPS IN TROUBLE") : "ALL CLEAR"
                color: root.problemCount > 0 ? root.urgent : root.foreground
                font.family: root.fontFamily
                font.pixelSize: Style.font.subtitle
                font.bold: true
              }
              Text {
                visible: root.showTotals
                textFormat: Text.PlainText
                text: (root.totals.procs !== undefined)
                  ? root.totals.procs + " processes · " + root.totals.groups + " groups · "
                    + root.fmtCpu(root.totals.cpu || 0) + " cpu · " + root.fmtMem(root.totals.mem || 0)
                    + (root.totals.load !== undefined ? " · load " + root.totals.load : "")
                  : ""
                color: root.dim
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption
              }
            }
          }

          // ------------------------------------------------------- mode chips
          Row {
            spacing: Style.space(8)
            Repeater {
              model: [
                { id: "all", label: "All" },
                { id: "issues", label: "Trouble" },
                { id: "cpu", label: "CPU" },
                { id: "mem", label: "RAM" }
              ]
              delegate: Rectangle {
                required property var modelData
                property bool on: root.mode === modelData.id
                width: chipText.implicitWidth + Style.space(24)
                height: Style.space(26)
                radius: height / 2
                color: on ? Util.alpha(root.accent, 0.22) : Util.alpha(root.foreground, 0.05)
                border.width: 1
                border.color: on ? root.accent : Util.alpha(root.foreground, 0.15)

                Text {
                  id: chipText
                  anchors.centerIn: parent
                  textFormat: Text.PlainText
                  text: parent.modelData.label
                  color: parent.on ? root.accent : root.foreground
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.caption
                  font.bold: parent.on
                }
                MouseArea {
                  anchors.fill: parent
                  onClicked: {
                    root.mode = modelData.id
                    root.cursor = -1
                    if (modelData.id === "cpu" || modelData.id === "mem") { /* sorted by mode */ }
                  }
                }
              }
            }
          }

          // ---------------------------------------------------------- search
          TextField {
            id: searchField
            width: parent.width
            placeholderText: "Search processes  ( / )"
            text: root.searchText
            onTextEdited: root.searchText = text
            onActiveFocusChanged: keyCatcher.blocked = activeFocus
          }

          // ----------------------------------------------------- empty/error
          Text {
            visible: root.errorText !== ""
            textFormat: Text.PlainText
            text: "Probe error: " + root.errorText
            color: root.urgent
            font.family: root.fontFamily
            font.pixelSize: Style.font.bodySmall
          }
          Text {
            visible: root.errorText === "" && !root.loading && root.visibleGroups.length === 0
            textFormat: Text.PlainText
            text: root.searchText !== "" ? "No match for \"" + root.searchText + "\"" : "No processes"
            color: root.dim
            font.family: root.fontFamily
            font.pixelSize: Style.font.bodySmall
          }
          Text {
            visible: root.loading && root.groups.length === 0
            textFormat: Text.PlainText
            text: "Reading processes…"
            color: root.dim
            font.family: root.fontFamily
            font.pixelSize: Style.font.bodySmall
          }

          // ------------------------------------------------------------ rows
          Repeater {
            model: root.visibleGroups
            delegate: Column {
              id: row
              required property var modelData
              required property int index
              property var g: modelData
              property bool hot: rowMouse.containsMouse
              property bool isOpen: root.expanded === g.id
              property bool isCursor: root.cursor === row.index
              width: parent.width
              spacing: 0

              Rectangle {
                width: parent.width
                implicitHeight: rowBody.implicitHeight + Style.space(16)
                radius: Style.space(10)
                color: row.isCursor ? Util.alpha(root.accent, 0.14)
                  : row.hot ? Util.alpha(root.foreground, 0.06)
                  : Util.alpha(root.foreground, 0.03)
                border.width: 1
                border.color: row.isOpen ? Util.alpha(root.accent, 0.55)
                  : g.severity === "critical" ? Util.alpha(root.urgent, 0.55)
                  : g.severity === "warning" ? Util.alpha(sevColor("warning"), 0.45)
                  : Util.alpha(root.foreground, 0.12)

                Column {
                  id: rowBody
                  width: parent.width - Style.space(20)
                  x: Style.space(10)
                  y: Style.space(8)
                  spacing: Style.space(6)

                  Row {
                    width: parent.width
                    spacing: Style.space(9)

                    Rectangle {
                      width: Style.space(9)
                      height: Style.space(9)
                      radius: 5
                      color: sevColor(g.severity)
                      anchors.verticalCenter: parent.verticalCenter
                    }

                    Text {
                      textFormat: Text.PlainText
                      text: g.label
                      elide: Text.ElideRight
                      width: Math.max(Style.space(120), parent.width - rowFigs.width - rowActs.width - parent.spacing * 3)
                      color: root.foreground
                      font.family: root.fontFamily
                      font.pixelSize: Style.font.body
                      font.bold: g.severity !== "ok"
                      anchors.verticalCenter: parent.verticalCenter
                    }

                    Row {
                      id: rowFigs
                      spacing: Style.space(10)
                      anchors.verticalCenter: parent.verticalCenter

                      Text {
                        textFormat: Text.PlainText
                        text: g.nproc > 1 ? "×" + g.nproc : ""
                        color: root.dim
                        font.family: root.fontFamily
                        font.pixelSize: Style.font.caption
                        anchors.verticalCenter: parent.verticalCenter
                      }
                      Text {
                        textFormat: Text.PlainText
                        text: root.fmtCpu(g.cpu)
                        color: g.cpu >= 90 ? root.urgent : root.dim
                        font.family: root.fontFamily
                        font.pixelSize: Style.font.bodySmall
                        font.bold: g.cpu >= 90
                        anchors.verticalCenter: parent.verticalCenter
                      }
                      Text {
                        textFormat: Text.PlainText
                        text: root.fmtMem(g.mem)
                        color: root.dim
                        font.family: root.fontFamily
                        font.pixelSize: Style.font.bodySmall
                        anchors.verticalCenter: parent.verticalCenter
                      }
                    }

                    Row {
                      id: rowActs
                      spacing: Style.space(6)
                      anchors.verticalCenter: parent.verticalCenter

                      Button {
                        text: "TERM"
                        fontSize: Style.font.caption
                        horizontalPadding: Style.space(9)
                        verticalPadding: Style.space(3)
                        foreground: root.foreground
                        enabled: !root.busy
                        opacity: root.busy ? 0.4 : 1
                        onClicked: root.askKillGroup(row.g)
                      }
                      Button {
                        text: "KILL"
                        fontSize: Style.font.caption
                        horizontalPadding: Style.space(9)
                        verticalPadding: Style.space(3)
                        foreground: root.urgent
                        enabled: !root.busy
                        opacity: root.busy ? 0.4 : 1
                        onClicked: root.askForceGroup(row.g)
                      }
                    }
                  }

                  // cpu bar
                  Rectangle {
                    width: parent.width
                    height: Style.space(4)
                    radius: 2
                    color: Util.alpha(root.foreground, 0.10)

                    Rectangle {
                      width: Math.max(parent.height, Math.min(parent.width, parent.width * Math.min(1, g.cpu / 150)))
                      height: parent.height
                      radius: 2
                      color: g.cpu >= 90 ? root.urgent : g.cpu >= 40 ? sevColor("warning") : root.accent
                    }
                  }

                  // badges
                  Row {
                    visible: (g.issues && g.issues.length > 0) || isOpen
                    spacing: Style.space(6)

                    Repeater {
                      model: (g.issues || [])
                      delegate: Rectangle {
                        required property string modelData
                        width: issueTxt.implicitWidth + Style.space(16)
                        height: Style.space(20)
                        radius: 4
                        color: Util.alpha(root.urgent, 0.18)
                        border.width: 1
                        border.color: Util.alpha(root.urgent, 0.5)
                        Text {
                          id: issueTxt
                          anchors.centerIn: parent
                          textFormat: Text.PlainText
                          text: modelData === "runaway" ? "BUSY LOOP?" : modelData.toUpperCase()
                          color: root.urgent
                          font.family: root.fontFamily
                          font.pixelSize: Style.font.caption
                          font.bold: true
                        }
                      }
                    }
                    Text {
                      visible: isOpen
                      textFormat: Text.PlainText
                      text: "age " + root.fmtAge(g.age) + " · root pid " + g.rootPid + " · " + g.threads + " threads"
                      color: root.dim
                      font.family: root.fontFamily
                      font.pixelSize: Style.font.caption
                    }
                  }

                  // expanded: processes
                  Column {
                    visible: row.isOpen
                    width: parent.width
                    spacing: Style.space(4)

                    Repeater {
                      model: (row.g.procs || [])
                      delegate: Row {
                        required property var modelData
                        property var p: modelData
                        width: parent.width
                        spacing: Style.space(8)

                        Text {
                          textFormat: Text.PlainText
                          text: String(p.pid)
                          color: root.dim
                          font.family: root.fontFamily
                          font.pixelSize: Style.font.caption
                          width: Style.space(64)
                          anchors.verticalCenter: parent.verticalCenter
                        }
                        Rectangle {
                          width: Style.space(20)
                          height: Style.space(16)
                          radius: 3
                          color: (p.stateCode === "Z" || p.stateCode === "D") ? Util.alpha(root.urgent, 0.2) : Util.alpha(root.foreground, 0.07)
                          anchors.verticalCenter: parent.verticalCenter
                          Text {
                            anchors.centerIn: parent
                            textFormat: Text.PlainText
                            text: root.stateGlyph(p.stateCode)
                            color: (p.stateCode === "Z" || p.stateCode === "D") ? root.urgent : root.dim
                            font.family: root.fontFamily
                            font.pixelSize: Style.font.caption
                            font.bold: true
                          }
                        }
                        Text {
                          textFormat: Text.PlainText
                          text: root.fmtCpu(p.cpu)
                          color: p.cpu >= 90 ? root.urgent : root.dim
                          font.family: root.fontFamily
                          font.pixelSize: Style.font.caption
                          width: Style.space(52)
                          anchors.verticalCenter: parent.verticalCenter
                        }
                        Text {
                          textFormat: Text.PlainText
                          text: root.fmtMem(p.rss)
                          color: root.dim
                          font.family: root.fontFamily
                          font.pixelSize: Style.font.caption
                          width: Style.space(58)
                          anchors.verticalCenter: parent.verticalCenter
                        }
                        Text {
                          textFormat: Text.PlainText
                          text: p.preview || p.label || ""
                          elide: Text.ElideRight
                          width: parent.width - Style.space(64) - Style.space(20) - Style.space(52) - Style.space(58) - Style.space(70) - parent.spacing * 5
                          color: p.stateCode === "Z" ? root.urgent : root.dim
                          font.family: root.fontFamily
                          font.pixelSize: Style.font.caption
                          anchors.verticalCenter: parent.verticalCenter
                        }
                        Button {
                          text: "T"
                          fontSize: Style.font.caption
                          horizontalPadding: Style.space(7)
                          verticalPadding: Style.space(2)
                          foreground: root.foreground
                          enabled: !root.busy
                          opacity: root.busy ? 0.4 : 1
                          anchors.verticalCenter: parent.verticalCenter
                          onClicked: root.askKillProc(row.g, p)
                        }
                        Button {
                          text: "K"
                          fontSize: Style.font.caption
                          horizontalPadding: Style.space(7)
                          verticalPadding: Style.space(2)
                          foreground: root.urgent
                          enabled: !root.busy
                          opacity: root.busy ? 0.4 : 1
                          anchors.verticalCenter: parent.verticalCenter
                          onClicked: {
                            root.confirm = { label: row.g.label + " · PID " + p.pid, pids: [p.pid], n: 1, sig: "kill" }
                          }
                        }
                      }
                    }
                  }
                }

                MouseArea {
                  id: rowMouse
                  anchors.fill: parent
                  hoverEnabled: true
                  z: -1
                  onClicked: {
                    root.cursor = row.index
                    root.expanded = row.isOpen ? "" : row.g.id
                  }
                }
              }
              Item { width: 1; height: Style.space(10) }
            }
          }

          // ------------------------------------------------------------ toast
          Rectangle {
            visible: root.toast !== ""
            width: parent.width
            implicitHeight: toastText.implicitHeight + Style.space(16)
            radius: Style.space(8)
            color: Util.alpha(root.accent, 0.16)
            border.width: 1
            border.color: Util.alpha(root.accent, 0.5)

            Text {
              id: toastText
              anchors.centerIn: parent
              width: parent.width - Style.space(20)
              horizontalAlignment: Text.AlignHCenter
              textFormat: Text.PlainText
              text: root.toast
              color: root.accent
              font.family: root.fontFamily
              font.pixelSize: Style.font.bodySmall
              font.bold: true
              wrapMode: Text.WordWrap
            }
          }
        }
      }

      // ------------------------------------------------------- confirm dialog
      ConfirmDialog {
        anchors.fill: parent
        opened: root.confirm !== null
        message: root.confirm !== null
          ? (root.confirm.sig === "kill"
              ? "Force-kill " + root.confirm.n + (root.confirm.n === 1 ? " process" : " processes") + " of " + root.confirm.label + "?\n\nSIGKILL cannot be caught: unsaved data will be lost."
              : "Stop " + root.confirm.n + (root.confirm.n === 1 ? " process" : " processes") + " of " + root.confirm.label + "?\n\nSIGTERM first, SIGKILL after 2s if it survives.")
          : ""
        confirmText: root.confirm !== null && root.confirm.sig === "kill" ? "Force kill" : "Stop"
        cancelText: "Cancel"
        onConfirmed: root.runConfirm()
        onCanceled: root.confirm = null
      }
    }
  }
}

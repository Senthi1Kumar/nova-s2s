pragma ComponentBehavior: Bound

import QtQuick
import NovaUI

/*
 * Orb — the one place this HMI spends its boldness.
 *
 * A ring of 64 bars driven by a 24-band energy vector, mirrored left/right so
 * the figure stays symmetric and readable at a glance. Colour is bound to the
 * assistant state, not to the audio, so a user across the room can tell
 * "listening" from "speaking" without reading a word.
 *
 * Deliberately built from plain Rectangles rather than a fragment shader:
 * no qsb toolchain in the build, and it stays cheap on an embedded GPU.
 */
Item {
    id: root

    property real  level: 0.0            // 0..1 smoothed envelope
    property var   bands: []             // 24 floats, 0..1
    property color accent: Theme.cyan
    property string phase: "idle"        // idle|listening|thinking|speaking|error
    property bool  reducedMotion: false

    readonly property int  barCount: 64
    readonly property real ringRadius: Math.min(width, height) * 0.26
    readonly property real barWidth: Math.max(2, ringRadius * 0.055)
    readonly property real coreSize: ringRadius * 1.18

    Behavior on accent { ColorAnimation { duration: 420; easing.type: Easing.OutCubic } }

    function barValue(i) {
        if (!bands || bands.length === 0) return 0
        const n = bands.length
        const half = barCount / 2
        const p = i < half ? i / half : (barCount - i) / half   // 0 → 1 → 0
        return bands[Math.min(n - 1, Math.floor(p * n))]
    }

    // ---- Halo ------------------------------------------------------------
    // Three stacked translucent discs. Cheaper than a blur pass and it reads
    // as depth rather than as a glow filter.
    Repeater {
        model: 3
        delegate: Rectangle {
            id: halo
            required property int index
            anchors.centerIn: parent
            readonly property real k: 1.9 + halo.index * 0.85
            width: root.ringRadius * k * (1 + root.level * 0.22)
            height: width
            radius: width / 2
            color: root.accent
            opacity: (0.055 - halo.index * 0.014) * (0.45 + root.level * 0.55)
            Behavior on width { NumberAnimation { duration: 160; easing.type: Easing.OutQuad } }
        }
    }

    // ---- Band ring -------------------------------------------------------
    Repeater {
        model: root.barCount
        delegate: Item {
            id: spoke
            required property int index
            width: root.width
            height: root.height
            rotation: spoke.index * (360 / root.barCount)

            Rectangle {
                anchors.horizontalCenter: parent.horizontalCenter
                width: root.barWidth
                height: root.ringRadius * 0.14
                       + root.barValue(spoke.index) * root.ringRadius * 0.62
                y: parent.height / 2 - root.ringRadius - height
                radius: width / 2
                color: root.accent
                opacity: 0.28 + root.barValue(spoke.index) * 0.72

                Behavior on height {
                    enabled: !root.reducedMotion
                    NumberAnimation { duration: 90; easing.type: Easing.OutQuad }
                }
            }
        }
    }

    // ---- Thinking sweep --------------------------------------------------
    Item {
        id: sweep
        anchors.fill: parent
        visible: root.phase === "thinking"
        opacity: visible ? 1 : 0
        Behavior on opacity { NumberAnimation { duration: 240 } }

        Repeater {
            model: 3
            delegate: Item {
                id: dot
                required property int index
                width: sweep.width
                height: sweep.height
                rotation: dot.index * 120
                Rectangle {
                    anchors.horizontalCenter: parent.horizontalCenter
                    y: sweep.height / 2 - root.ringRadius * 1.62
                    width: root.barWidth * 1.6
                    height: width
                    radius: width / 2
                    color: root.accent
                    opacity: 1.0 - dot.index * 0.28
                }
            }
        }

        RotationAnimator {
            target: sweep
            from: 0; to: 360
            duration: 2600
            loops: Animation.Infinite
            running: sweep.visible && !root.reducedMotion
        }
    }

    // ---- Core ------------------------------------------------------------
    Rectangle {
        id: core
        anchors.centerIn: parent
        width: root.coreSize
        height: width
        radius: width / 2
        color: Qt.rgba(root.accent.r, root.accent.g, root.accent.b, 0.09)
        border.color: root.accent
        border.width: Math.max(1, root.ringRadius * 0.012)
        scale: 1 + root.level * 0.09
        Behavior on scale { NumberAnimation { duration: 120; easing.type: Easing.OutQuad } }

        // Idle breath — the only motion when nothing is happening.
        SequentialAnimation on opacity {
            running: root.phase === "idle" && !root.reducedMotion
            loops: Animation.Infinite
            NumberAnimation { to: 0.45; duration: 2200; easing.type: Easing.InOutSine }
            NumberAnimation { to: 1.00; duration: 2200; easing.type: Easing.InOutSine }
        }
        onVisibleChanged: if (root.phase !== "idle") opacity = 1
    }

    Rectangle {
        anchors.centerIn: parent
        width: root.coreSize * (0.13 + root.level * 0.30)
        height: width
        radius: width / 2
        color: root.accent
        opacity: 0.85
        Behavior on width { NumberAnimation { duration: 110; easing.type: Easing.OutQuad } }
    }
}

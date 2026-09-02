import QtQuick
import NovaUI

Rectangle {
    id: root
    property bool open: false
    width: Math.min(parent.width * 0.5, 460)
    height: parent.height
    x: open ? parent.width - width : parent.width
    color: "#0b0e14"
    border.color: Theme.hairline
    visible: x < parent.width
    Behavior on x { NumberAnimation { duration: 180 } }

    function fmt(ms) {
        if (ms === undefined || ms === null || ms <= 0) return "—"
        return Math.round(ms) + " ms"
    }

    component MetricCard: Rectangle {
        property string lbl
        property string val
        width: (cards.width - 10) / 2
        height: 72
        radius: 12
        color: Theme.surface
        border.color: Theme.hairline
        Column {
            anchors.fill: parent
            anchors.margins: 12
            spacing: 6
            Text {
                text: lbl
                font.family: Theme.mono
                font.pixelSize: 10
                font.letterSpacing: 1.1
                color: Theme.textLo
            }
            Text {
                text: val
                font.family: Theme.display
                font.pixelSize: 18
                font.weight: Font.DemiBold
                color: Theme.textHi
            }
        }
    }

    Flickable {
        anchors.fill: parent
        contentWidth: width
        contentHeight: col.implicitHeight + 24
        clip: true

        Column {
            id: col
            width: parent.width - 32
            x: 16
            y: 16
            spacing: 12

            Row {
                width: parent.width
                Text {
                    text: "OPS · latencies"
                    font.family: Theme.display
                    font.pixelSize: 16
                    font.weight: Font.DemiBold
                    color: Theme.textHi
                    width: parent.width - 40
                }
                Text {
                    text: "✕"
                    color: Theme.textMid
                    MouseArea { anchors.fill: parent; anchors.margins: -8; onClicked: nova.closeDrawers() }
                }
            }

            Text {
                width: parent.width
                wrapMode: Text.Wrap
                text: "E2E is speech-stop → first audible. LLM is generate-only (TTFT + decode), not TTS."
                font.family: Theme.text
                font.pixelSize: 11
                color: Theme.textLo
            }

            Grid {
                id: cards
                width: parent.width
                columns: 2
                columnSpacing: 10
                rowSpacing: 10

                MetricCard { lbl: "E2E / audible"; val: fmt(nova.e2eMs) }
                MetricCard { lbl: "STT"; val: fmt(nova.sttMs) }
                MetricCard { lbl: "LLM total"; val: fmt(nova.llmMs) }
                MetricCard { lbl: "LLM TTFT"; val: fmt(nova.llmTtftMs) }
                MetricCard { lbl: "LLM decode"; val: fmt(nova.llmDecodeMs) }
                MetricCard { lbl: "TTS synth"; val: fmt(nova.ttsMs) }
                MetricCard { lbl: "STT path"; val: nova.sttPath || "—" }
                MetricCard { lbl: "Runtime"; val: nova.llmMode === "openrouter" ? nova.cloudLabel : "Hailo" }
            }

            Text {
                width: parent.width
                wrapMode: Text.Wrap
                text: nova.backendLabel
                font.family: Theme.mono
                font.pixelSize: 11
                color: Theme.textMid
            }
        }
    }
}

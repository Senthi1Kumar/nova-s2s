pragma ComponentBehavior: Bound

import QtQuick
import NovaUI

Item {
    id: shell

    ListModel { id: transcriptModel }

    Connections {
        target: nova
        function onTranscriptAdded(role, text) {
            transcriptModel.append({ role: role, text: text })
            if (transcriptModel.count > 80)
                transcriptModel.remove(0)
        }
        function onCleared() { transcriptModel.clear() }
    }

    TopBar {
        id: top
        width: parent.width
        anchors.top: parent.top
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.margins: Theme.u * 2
        z: 10
    }

    Column {
        id: stage
        anchors.horizontalCenter: parent.horizontalCenter
        anchors.verticalCenter: parent.verticalCenter
        anchors.verticalCenterOffset: -parent.height * 0.04
        spacing: 18
        width: Math.min(parent.width * 0.72, 420)

        Item {
            id: orbBox
            width: Math.min(340, parent.width)
            height: width
            anchors.horizontalCenter: parent.horizontalCenter

            Orb {
                anchors.fill: parent
                level: nova.level
                bands: nova.bands
                phase: nova.phase
                accent: Theme.accentFor(nova.phase)
            }
        }

        Text {
            anchors.horizontalCenter: parent.horizontalCenter
            text: Theme.labelFor(nova.phase)
            font.family: Theme.text
            font.pixelSize: 13
            font.letterSpacing: 3.2
            font.capitalization: Font.AllUppercase
            color: Theme.accentFor(nova.phase)
        }

        Text {
            anchors.horizontalCenter: parent.horizontalCenter
            width: parent.width
            horizontalAlignment: Text.AlignHCenter
            text: nova.toolCapsule
            visible: nova.toolCapsule.length > 0 && nova.phase === "thinking"
            font.family: Theme.mono
            font.pixelSize: 11
            color: Theme.textMid
        }
    }

    Rectangle {
        id: pill
        visible: nova.toolCapsule.length > 0
        anchors.horizontalCenter: parent.horizontalCenter
        anchors.top: top.bottom
        anchors.topMargin: 8
        height: 32
        width: pillTxt.implicitWidth + 28
        radius: 16
        color: Qt.rgba(1, 1, 1, 0.07)
        border.color: Qt.rgba(1, 1, 1, 0.12)
        opacity: 1
        Text {
            id: pillTxt
            anchors.centerIn: parent
            text: nova.toolCapsule
            font.family: Theme.text
            font.pixelSize: 12
            color: Theme.textMid
        }
        Timer {
            id: pillFade
            interval: 1800
            onTriggered: nova.clearToolCapsule()
        }
        Connections {
            target: nova
            function onToolCapsuleStatusChanged() {
                var st = nova.toolCapsuleStatus
                if (st === "done" || st === "failed" || st === "complete" || st === "completed")
                    pillFade.restart()
                else
                    pillFade.stop()
            }
            function onLiveUserChanged() { pillFade.stop() }
        }
    }

    LiveStrip {
        id: live
        width: Math.min(520, parent.width - 32)
        anchors.horizontalCenter: parent.horizontalCenter
        anchors.bottom: parent.bottom
        anchors.bottomMargin: Theme.u * 4
        z: 8
    }

    Text {
        anchors.left: parent.left
        anchors.bottom: parent.bottom
        anchors.margins: 16
        text: "Powered by Elevatics AI"
        font.family: Theme.text
        font.pixelSize: 11
        color: Theme.textLo
        opacity: 0.7
    }

    ChatDrawer {
        z: 30
        open: nova.chatOpen
        model: transcriptModel
    }
    OpsDrawer {
        z: 30
        open: nova.opsOpen
    }
    SettingsPanel {
        z: 30
        open: nova.settingsOpen
    }
}

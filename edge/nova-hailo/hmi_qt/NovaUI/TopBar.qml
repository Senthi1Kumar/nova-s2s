import QtQuick
import NovaUI

Item {
    id: root
    height: Theme.u * 7

    Row {
        anchors.left: parent.left
        anchors.verticalCenter: parent.verticalCenter
        spacing: 10

        Rectangle {
            width: 10; height: 10; radius: 5
            anchors.verticalCenter: parent.verticalCenter
            color: Theme.accentFor(nova.phase)
            Behavior on color { ColorAnimation { duration: 280 } }
        }

        Text {
            text: "NOVA"
            anchors.verticalCenter: parent.verticalCenter
            font.family: Theme.display
            font.pixelSize: 15
            font.weight: Font.DemiBold
            font.letterSpacing: 3.2
            color: Theme.textHi
        }

        Text {
            text: nova.llmMode === "openrouter" ? "Cloud " + nova.cloudLabel : "Local Hailo"
            anchors.verticalCenter: parent.verticalCenter
            font.family: Theme.mono
            font.pixelSize: 11
            font.letterSpacing: 1.2
            color: Theme.textMid
        }
    }

    Text {
        anchors.horizontalCenter: parent.horizontalCenter
        anchors.verticalCenter: parent.verticalCenter
        text: Theme.labelFor(nova.phase).toUpperCase()
        font.family: Theme.mono
        font.pixelSize: 11
        font.letterSpacing: 2.4
        color: Theme.accentFor(nova.phase)
    }

    Row {
        anchors.right: parent.right
        anchors.verticalCenter: parent.verticalCenter
        spacing: Theme.u

        Repeater {
            model: [
                { id: "ops", label: "OPS" },
                { id: "chat", label: "Chat" },
                { id: "set", label: "⚙" }
            ]
            delegate: Rectangle {
                width: 40
                height: 36
                radius: 10
                color: Theme.surface
                border.color: Theme.hairline
                Text {
                    anchors.centerIn: parent
                    text: modelData.label
                    color: Theme.textMid
                    font.family: Theme.mono
                    font.pixelSize: modelData.id === "set" ? 14 : 10
                    font.letterSpacing: 0.8
                }
                MouseArea {
                    anchors.fill: parent
                    cursorShape: Qt.PointingHandCursor
                    onClicked: {
                        if (modelData.id === "ops") nova.toggleOps()
                        else if (modelData.id === "chat") nova.toggleChat()
                        else nova.toggleSettings()
                    }
                }
            }
        }
    }
}

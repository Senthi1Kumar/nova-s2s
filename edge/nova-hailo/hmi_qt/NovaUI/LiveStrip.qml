import QtQuick
import NovaUI

Item {
    id: root
    visible: nova.liveVisible && (nova.liveUser.length + nova.liveAssistant.length) > 0
    height: col.implicitHeight

    Timer {
        id: fade
        interval: 4200
        onTriggered: nova.hideLive()
    }

    Connections {
        target: nova
        function onLiveUserChanged() { fade.restart() }
        function onLiveAssistantChanged() { fade.restart() }
    }

    Column {
        id: col
        width: parent.width
        spacing: 8

        Rectangle {
            visible: nova.liveUser.length > 0
            width: parent.width
            height: youCol.implicitHeight + 22
            radius: 14
            color: Theme.surface
            border.color: Theme.hairline
            Rectangle {
                width: 3; height: parent.height
                color: Theme.cyan
                radius: 2
            }
            Column {
                id: youCol
                anchors.left: parent.left
                anchors.right: parent.right
                anchors.verticalCenter: parent.verticalCenter
                anchors.leftMargin: 14
                anchors.rightMargin: 12
                spacing: 4
                Text {
                    text: "YOU"
                    font.family: Theme.mono
                    font.pixelSize: 10
                    font.letterSpacing: 1.6
                    color: Theme.textLo
                }
                Text {
                    width: parent.width
                    text: nova.liveUser
                    wrapMode: Text.Wrap
                    font.family: Theme.text
                    font.pixelSize: 15
                    color: Theme.textMid
                }
            }
        }

        Rectangle {
            visible: nova.liveAssistant.length > 0
            width: parent.width
            height: asCol.implicitHeight + 22
            radius: 14
            color: Theme.surface
            border.color: Theme.hairline
            Rectangle {
                width: 3; height: parent.height
                color: Theme.violet
                radius: 2
            }
            Column {
                id: asCol
                anchors.left: parent.left
                anchors.right: parent.right
                anchors.verticalCenter: parent.verticalCenter
                anchors.leftMargin: 14
                anchors.rightMargin: 12
                spacing: 4
                Text {
                    text: "NOVA"
                    font.family: Theme.mono
                    font.pixelSize: 10
                    font.letterSpacing: 1.6
                    color: Theme.cyan
                }
                Text {
                    width: parent.width
                    text: nova.liveAssistant
                    wrapMode: Text.Wrap
                    font.family: Theme.text
                    font.pixelSize: 15
                    color: Theme.textHi
                }
            }
        }
    }
}

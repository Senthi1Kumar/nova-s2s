import QtQuick
import NovaUI

Rectangle {
    id: root
    property bool open: false
    property alias model: list.model
    width: Math.min(parent.width * 0.42, 380)
    height: parent.height
    x: open ? parent.width - width : parent.width
    color: "#0b0e14"
    border.color: Theme.hairline
    visible: x < parent.width
    Behavior on x { NumberAnimation { duration: 180 } }

    Column {
        anchors.fill: parent
        anchors.margins: 16
        spacing: 12

        Row {
            width: parent.width
            Text {
                text: "Conversation"
                font.family: Theme.display
                font.pixelSize: 16
                font.weight: Font.DemiBold
                color: Theme.textHi
                width: parent.width - 40
            }
            Text {
                text: "✕"
                color: Theme.textMid
                font.pixelSize: 16
                MouseArea { anchors.fill: parent; anchors.margins: -8; onClicked: nova.closeDrawers() }
            }
        }

        ListView {
            id: list
            width: parent.width
            height: root.height - 72
            clip: true
            spacing: 10
            delegate: Column {
                id: row
                required property string role
                required property string text
                width: list.width
                spacing: 4
                Text {
                    text: row.role === "user" ? "YOU" : (row.role === "tool" ? "TOOL" : "NOVA")
                    font.family: Theme.mono
                    font.pixelSize: 10
                    font.letterSpacing: 1.4
                    color: row.role === "tool" ? Theme.orange : (row.role === "user" ? Theme.textLo : Theme.cyan)
                }
                Rectangle {
                    width: list.width * 0.92
                    height: body.implicitHeight + 16
                    radius: 12
                    color: row.role === "user" ? Theme.surface : (row.role === "tool" ? "#1a1610" : "#181d2a")
                    border.color: Theme.hairline
                    Text {
                        id: body
                        anchors.left: parent.left
                        anchors.right: parent.right
                        anchors.verticalCenter: parent.verticalCenter
                        anchors.margins: 10
                        text: row.text
                        wrapMode: Text.Wrap
                        font.family: Theme.text
                        font.pixelSize: 13
                        color: Theme.textHi
                    }
                }
            }
        }
    }
}

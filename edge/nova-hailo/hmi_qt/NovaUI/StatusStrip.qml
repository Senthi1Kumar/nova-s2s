import QtQuick
import NovaUI

Item {
    id: root
    property string phase: "idle"
    property string link: "offline"      // online | offline | demo
    property int    latencyMs: 0
    implicitHeight: Theme.u * 6

    Row {
        anchors.left: parent.left
        anchors.verticalCenter: parent.verticalCenter
        spacing: Theme.u

        Rectangle {
            width: 7; height: 7; radius: 3.5
            anchors.verticalCenter: parent.verticalCenter
            color: Theme.accentFor(root.phase)
            Behavior on color { ColorAnimation { duration: 300 } }
        }

        Text {
            text: Theme.labelFor(root.phase)
            anchors.verticalCenter: parent.verticalCenter
            font.family: Theme.text
            font.pixelSize: 13
            font.weight: Font.Medium
            color: Theme.textHi
        }
    }

    Row {
        anchors.right: parent.right
        anchors.verticalCenter: parent.verticalCenter
        spacing: Theme.u * 2

        Text {
            visible: root.latencyMs > 0
            text: root.latencyMs + " ms"
            anchors.verticalCenter: parent.verticalCenter
            font.family: Theme.mono
            font.pixelSize: 11
            color: root.latencyMs > 800 ? Theme.orange : Theme.textLo
        }

        Text {
            text: root.link.toUpperCase()
            anchors.verticalCenter: parent.verticalCenter
            font.family: Theme.mono
            font.pixelSize: 10
            font.letterSpacing: 1.4
            color: root.link === "online" ? Theme.green
                 : root.link === "demo"   ? Theme.yellow
                                          : Theme.textLo
        }
    }

    Rectangle {
        anchors.bottom: parent.bottom
        width: parent.width
        height: 1
        color: Theme.hairline
    }
}

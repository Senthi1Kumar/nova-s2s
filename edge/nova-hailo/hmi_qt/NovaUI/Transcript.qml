pragma ComponentBehavior: Bound

import QtQuick
import NovaUI

Item {
    id: root
    property alias model: list.model

    ListView {
        id: list
        anchors.fill: parent
        spacing: Theme.u * 1.5
        clip: true
        verticalLayoutDirection: ListView.BottomToTop

        delegate: Column {
            id: line
            required property string role
            required property string text
            width: list.width
            spacing: 4

            Text {
                text: line.role === "user" ? "YOU" : "NOVA"
                font.family: Theme.mono
                font.pixelSize: 10
                font.letterSpacing: 1.6
                color: line.role === "user" ? Theme.textLo : Theme.cyan
            }

            Text {
                width: parent.width
                text: line.text
                wrapMode: Text.Wrap
                font.family: Theme.text
                font.pixelSize: 15
                lineHeight: 1.35
                color: line.role === "user" ? Theme.textMid : Theme.textHi
            }
        }
    }

    // Empty state is an invitation, not a shrug.
    Text {
        anchors.centerIn: parent
        visible: list.count === 0
        text: "Say the wake word to start"
        font.family: Theme.text
        font.pixelSize: 13
        color: Theme.textLo
    }
}

pragma ComponentBehavior: Bound

import QtQuick
import NovaUI

/*
 * Shell — the entire HMI as a plain Item.
 *
 * Kept separate from Main.qml so the same surface can be hosted in an
 * ApplicationWindow on the bench, in a QQuickView on an embedded target, or
 * inside a larger cluster/IVI scene as one layer among several.
 */
Item {
    id: shell

    ListModel { id: transcriptModel }

    Connections {
        target: nova
        function onTranscriptAdded(role, text) {
            transcriptModel.insert(0, { role: role, text: text })
            if (transcriptModel.count > 40) transcriptModel.remove(40)
        }
        function onCleared() { transcriptModel.clear() }
    }

    Item {
        anchors.fill: parent
        anchors.margins: Theme.u * 3

        StatusStrip {
            id: strip
            width: parent.width
            phase: nova.phase
            link: nova.link
            latencyMs: nova.latencyMs
        }

        Item {
            id: stage
            anchors.top: strip.bottom
            anchors.bottom: parent.bottom
            anchors.left: parent.left
            anchors.right: parent.right
            anchors.topMargin: Theme.u * 2

            readonly property bool wide: width > height * 1.3

            Orb {
                id: orb
                width: stage.wide ? stage.width * 0.44 : stage.width
                height: stage.wide ? stage.height : stage.height * 0.55
                anchors.left: parent.left
                anchors.top: parent.top
                level: nova.level
                bands: nova.bands
                phase: nova.phase
                accent: Theme.accentFor(nova.phase)
            }

            Item {
                id: rail
                anchors.left: stage.wide ? orb.right : parent.left
                anchors.top: stage.wide ? parent.top : orb.bottom
                anchors.right: parent.right
                anchors.bottom: parent.bottom
                anchors.leftMargin: stage.wide ? Theme.u * 3 : 0

                Text {
                    id: heading
                    text: "NOVA"
                    font.family: Theme.display
                    font.pixelSize: 34
                    font.weight: Font.Bold
                    font.letterSpacing: -0.8
                    color: Theme.textHi
                }

                Text {
                    id: sub
                    anchors.top: heading.bottom
                    anchors.topMargin: 2
                    text: "Local voice pipeline · " + nova.backendLabel
                    font.family: Theme.mono
                    font.pixelSize: 11
                    font.letterSpacing: 0.8
                    color: Theme.textLo
                }

                Transcript {
                    anchors.top: sub.bottom
                    anchors.topMargin: Theme.u * 3
                    anchors.bottom: parent.bottom
                    anchors.left: parent.left
                    anchors.right: parent.right
                    model: transcriptModel
                }
            }
        }
    }
}

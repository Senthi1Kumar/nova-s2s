import QtQuick
import QtQuick.Controls
import NovaUI

Rectangle {
    id: root
    property bool open: false
    width: Math.min(parent.width * 0.46, 420)
    height: parent.height
    x: open ? parent.width - width : parent.width
    color: "#0b0e14"
    border.color: Theme.hairline
    visible: x < parent.width
    Behavior on x { NumberAnimation { duration: 180 } }

    Flickable {
        anchors.fill: parent
        contentWidth: width
        contentHeight: col.implicitHeight + 32
        clip: true

        Column {
            id: col
            width: parent.width - 32
            x: 16
            y: 16
            spacing: Theme.u * 1.5

            Row {
                width: parent.width
                Text {
                    text: "Settings"
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
                text: "Runtime"
                color: Theme.textMid
                font.family: Theme.mono
                font.pixelSize: 10
                font.letterSpacing: 1.4
            }

            Row {
                spacing: Theme.u
                Repeater {
                    model: [
                        { id: "local", label: "Local Hailo" },
                        { id: "openrouter", label: "Cloud " + nova.cloudLabel }
                    ]
                    delegate: Rectangle {
                        width: 150
                        height: 32
                        radius: 8
                        color: nova.llmMode === modelData.id ? Theme.surfaceHi : "transparent"
                        border.color: nova.llmMode === modelData.id ? Theme.cyan : Theme.hairline
                        Text {
                            anchors.centerIn: parent
                            text: modelData.label
                            color: Theme.textHi
                            font.pixelSize: 12
                            font.family: Theme.text
                        }
                        MouseArea {
                            anchors.fill: parent
                            onClicked: nova.applyLlmSettings(modelData.id, nova.localHef, nova.orModel)
                        }
                    }
                }
            }

            Text {
                text: nova.llmMode === "openrouter" ? nova.cloudLabel + " model" : "Hailo HEF"
                color: Theme.textMid
                font.pixelSize: 12
            }

            ComboBox {
                id: picker
                width: parent.width
                model: nova.llmMode === "openrouter" ? nova.orModels : nova.localModels
                textRole: "label"
                valueRole: "id"
                onActivated: {
                    var id = picker.model[index].id
                    if (!id)
                        return
                    if (nova.llmMode === "openrouter") {
                        if (id === nova.orModel)
                            return
                        nova.applyLlmSettings("openrouter", nova.localHef, id)
                    } else {
                        if (id === nova.localHef)
                            return
                        nova.applyLlmSettings("local", id, nova.orModel)
                    }
                }
            }

            Text {
                visible: nova.llmMode === "openrouter" && !nova.hasOrKey
                text: "Set OPENROUTER_API_KEY in .env on the Pi."
                color: Theme.orange
                font.pixelSize: 11
                wrapMode: Text.Wrap
                width: parent.width
            }

            Rectangle { width: parent.width; height: 1; color: Theme.hairline }

            Text {
                text: "Noise gate  ·  RMS " + nova.gateRms.toFixed(3)
                color: Theme.textMid
                font.family: Theme.mono
                font.pixelSize: 10
                font.letterSpacing: 1.2
            }
            Slider {
                width: parent.width
                from: 0.002
                to: 0.08
                value: nova.gateRms
                onMoved: nova.setGateRms(value)
            }
            Text {
                text: "Higher = ignore quieter cabin noise (may drop soft speech)."
                color: Theme.textLo
                font.pixelSize: 11
                wrapMode: Text.Wrap
                width: parent.width
            }

            Row {
                spacing: 10
                Text {
                    text: "DTLN noise suppress"
                    color: Theme.textMid
                    font.pixelSize: 12
                    anchors.verticalCenter: parent.verticalCenter
                }
                Switch {
                    checked: nova.nsOn
                    onToggled: nova.setNsOn(checked)
                }
            }
            Text {
                visible: nova.nsOn
                text: "DTLN mix  ·  " + nova.nsStrength.toFixed(2)
                color: Theme.textMid
                font.family: Theme.mono
                font.pixelSize: 10
            }
            Slider {
                visible: nova.nsOn
                width: parent.width
                from: 0.1
                to: 1.0
                value: nova.nsStrength
                onMoved: nova.setNsStrength(value)
            }
            Text {
                visible: nova.nsOn
                text: "0.5 is the demo default. 1.0 often empties STT."
                color: Theme.textLo
                font.pixelSize: 11
                wrapMode: Text.Wrap
                width: parent.width
            }

            Text {
                text: nova.llmStatus
                visible: nova.llmStatus.length > 0
                color: Theme.textLo
                font.family: Theme.mono
                font.pixelSize: 11
            }

            Rectangle { width: parent.width; height: 1; color: Theme.hairline }

            Text {
                text: "Integrations"
                color: Theme.textMid
                font.family: Theme.mono
                font.pixelSize: 10
                font.letterSpacing: 1.4
            }

            // Read-only status + a single tap to connect/reconnect/disconnect.
            // Registering connectors stays in the browser settings UI —
            // typing a URL on a driver display while moving is unsafe.
            Row {
                width: parent.width
                spacing: Theme.u

                Column {
                    width: parent.width - 96
                    spacing: 2
                    Text {
                        text: "Google Workspace"
                        color: Theme.textHi
                        font.pixelSize: 12
                        font.family: Theme.text
                    }
                    Text {
                        text: nova.googleNeedsReauth
                              ? "Reconnect needed"
                              : (nova.googleConnected ? "Connected" : "Disconnected")
                        color: nova.googleNeedsReauth
                               ? Theme.orange
                               : (nova.googleConnected ? Theme.green : Theme.textLo)
                        font.pixelSize: 11
                    }
                }

                Rectangle {
                    id: googleBtn
                    readonly property bool healthy: nova.googleConnected && !nova.googleNeedsReauth
                    width: 96
                    height: 30
                    radius: 8
                    color: healthy ? Theme.surfaceHi : "transparent"
                    border.color: healthy ? Theme.hairline : Theme.cyan
                    Text {
                        anchors.centerIn: parent
                        text: googleBtn.healthy ? "Disconnect" : (nova.googleNeedsReauth ? "Reconnect" : "Connect")
                        color: googleBtn.healthy ? Theme.textMid : Theme.cyan
                        font.pixelSize: 11
                        font.family: Theme.text
                    }
                    MouseArea {
                        anchors.fill: parent
                        onClicked: googleBtn.healthy ? nova.disconnectGoogle() : nova.connectGoogle()
                    }
                }
            }

            Text {
                text: "Connectors"
                color: Theme.textMid
                font.family: Theme.mono
                font.pixelSize: 10
                font.letterSpacing: 1.4
            }
            Text {
                text: nova.connectorsEnabled > 0
                      ? (nova.connectorsEnabled + " enabled · " + nova.connectorsTools + " tools")
                      : "None"
                color: Theme.textHi
                font.pixelSize: 12
            }
            Text {
                text: "Add or remove connectors from the browser settings UI."
                color: Theme.textLo
                font.pixelSize: 11
                wrapMode: Text.Wrap
                width: parent.width
            }
        }
    }
}

"""Build the unsigned Shortcut. Sign on a Mac with:
shortcuts sign --mode anyone -i shortcut/unsigned.shortcut -o "shortcut/Sync to Lidarr.shortcut"
"""
import plistlib, uuid, os

TOKEN, COMBINED = str(uuid.uuid4()).upper(), str(uuid.uuid4()).upper()


def out(u, name):
    return {"OutputUUID": u, "Type": "ActionOutput", "OutputName": name}


def text(s, attachments=None):
    return {"Value": {"string": s, "attachmentsByRange": attachments or {}}, "WFSerializationType": "WFTextTokenString"}


actions = [
    # 0: token (asked on import)
    {"WFWorkflowActionIdentifier": "is.workflow.actions.gettext",
     "WFWorkflowActionParameters": {"UUID": TOKEN, "WFTextActionText": text("PASTE-TOKEN-HERE")}},
    # 1: all music in the library (add a filter in the Shortcuts app to narrow it)
    {"WFWorkflowActionIdentifier": "is.workflow.actions.filter.music",
     "WFWorkflowActionParameters": {}},
    # 2: album artist of each song
    {"WFWorkflowActionIdentifier": "is.workflow.actions.properties.music",
     "WFWorkflowActionParameters": {"WFContentItemPropertyName": "Album Artist"}},
    # 3: one per line
    {"WFWorkflowActionIdentifier": "is.workflow.actions.text.combine",
     "WFWorkflowActionParameters": {"UUID": COMBINED, "WFTextSeparator": "New Lines"}},
    # 4: POST to /ingest (URL asked on import)
    {"WFWorkflowActionIdentifier": "is.workflow.actions.downloadurl",
     "WFWorkflowActionParameters": {
         "WFURL": "https://your-host/ingest",
         "WFHTTPMethod": "POST",
         "WFHTTPBodyType": "File",
         "WFRequestVariable": {"Value": out(COMBINED, "Combined Text"), "WFSerializationType": "WFTextTokenAttachment"},
         "ShowHeaders": True,
         "WFHTTPHeaders": {"Value": {"WFDictionaryFieldValueItems": [{
             "WFItemType": 0,
             "WFKey": text("Authorization"),
             "WFValue": text("Bearer ￼", {"{7, 1}": out(TOKEN, "Text")}),
         }]}, "WFSerializationType": "WFDictionaryFieldValue"},
     }},
]

shortcut = {
    "WFWorkflowClientVersion": "2607.0.2",
    "WFWorkflowMinimumClientVersion": 900,
    "WFWorkflowMinimumClientVersionString": "900",
    "WFWorkflowIcon": {"WFWorkflowIconStartColor": 4282601983, "WFWorkflowIconGlyphNumber": 59511},
    "WFWorkflowTypes": [],
    "WFWorkflowInputContentItemClasses": [],
    "WFWorkflowOutputContentItemClasses": [],
    "WFWorkflowHasShortcutInputVariables": False,
    "WFWorkflowImportQuestions": [
        {"ActionIndex": 4, "Category": "Parameter", "ParameterKey": "WFURL",
         "DefaultValue": "https://your-host/ingest", "Text": "Your applemusic-lidarr-sync URL, ending in /ingest"},
        {"ActionIndex": 0, "Category": "Parameter", "ParameterKey": "WFTextActionText",
         "DefaultValue": "", "Text": "Your TOKEN (from the container's environment)"},
    ],
    "WFWorkflowActions": actions,
}

here = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(here, "unsigned.shortcut"), "wb") as f:
    plistlib.dump(shortcut, f, fmt=plistlib.FMT_BINARY)
print("wrote unsigned.shortcut")

"""Build the unsigned Shortcut. Sign on a Mac with:
shortcuts sign --mode anyone -i shortcut/unsigned.shortcut -o "shortcut/Sync to Lidarr.shortcut"
"""
import plistlib, uuid, os

SETUP, SPLIT, URL, TOKEN, FIND, ARTISTS, COMBINED = (str(uuid.uuid4()).upper() for _ in range(7))


def out(u, name):
    return {"OutputUUID": u, "Type": "ActionOutput", "OutputName": name}


def var(u, name):
    return {"Value": out(u, name), "WFSerializationType": "WFTextTokenAttachment"}


def text(s, attachments=None):
    return {"Value": {"string": s, "attachmentsByRange": attachments or {}}, "WFSerializationType": "WFTextTokenString"}


actions = [
    # 0: setup code from the admin page, "<url>/ingest <token>" (asked on import)
    {"WFWorkflowActionIdentifier": "is.workflow.actions.gettext",
     "WFWorkflowActionParameters": {"UUID": SETUP, "WFTextActionText": text("PASTE-SETUP-CODE-HERE")}},
    # 1-3: split it into the upload URL and the token
    {"WFWorkflowActionIdentifier": "is.workflow.actions.text.split",
     "WFWorkflowActionParameters": {"UUID": SPLIT, "text": var(SETUP, "Text"), "WFTextSeparator": "Spaces"}},
    {"WFWorkflowActionIdentifier": "is.workflow.actions.getitemfromlist",
     "WFWorkflowActionParameters": {"UUID": URL, "WFInput": var(SPLIT, "Split Text"), "WFItemSpecifier": "First Item"}},
    {"WFWorkflowActionIdentifier": "is.workflow.actions.getitemfromlist",
     "WFWorkflowActionParameters": {"UUID": TOKEN, "WFInput": var(SPLIT, "Split Text"), "WFItemSpecifier": "Last Item"}},
    # 4: all music in the library (add a filter in the Shortcuts app to narrow it)
    {"WFWorkflowActionIdentifier": "is.workflow.actions.filter.music",
     "WFWorkflowActionParameters": {"UUID": FIND}},
    # 5: album artist of each song
    {"WFWorkflowActionIdentifier": "is.workflow.actions.properties.music",
     "WFWorkflowActionParameters": {"UUID": ARTISTS, "WFInput": var(FIND, "Music"), "WFContentItemPropertyName": "Album Artist"}},
    # 6: one per line
    {"WFWorkflowActionIdentifier": "is.workflow.actions.text.combine",
     "WFWorkflowActionParameters": {"UUID": COMBINED, "text": var(ARTISTS, "Album Artist"), "WFTextSeparator": "New Lines"}},
    # 7: POST to the URL from the setup code, with its token
    {"WFWorkflowActionIdentifier": "is.workflow.actions.downloadurl",
     "WFWorkflowActionParameters": {
         "WFURL": text("\ufffc", {"{0, 1}": out(URL, "Item from List")}),
         "WFHTTPMethod": "POST",
         "WFHTTPBodyType": "File",
         "WFRequestVariable": var(COMBINED, "Combined Text"),
         "ShowHeaders": True,
         "WFHTTPHeaders": {"Value": {"WFDictionaryFieldValueItems": [{
             "WFItemType": 0,
             "WFKey": text("Authorization"),
             "WFValue": text("Bearer \ufffc", {"{7, 1}": out(TOKEN, "Item from List")}),
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
        {"ActionIndex": 0, "Category": "Parameter", "ParameterKey": "WFTextActionText", "DefaultValue": "",
         "Text": "Paste your setup code (from the admin page: Users → your name)"},
    ],
    "WFWorkflowActions": actions,
}

here = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(here, "unsigned.shortcut"), "wb") as f:
    plistlib.dump(shortcut, f, fmt=plistlib.FMT_BINARY)
print("wrote unsigned.shortcut")

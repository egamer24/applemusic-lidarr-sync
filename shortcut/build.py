"""Build the unsigned Shortcuts. Sign on a Mac with:
shortcuts sign --mode anyone -i shortcut/unsigned.shortcut -o "shortcut/Sync to Lidarr.shortcut"
shortcuts sign --mode anyone -i shortcut/unsigned-albums.shortcut -o "shortcut/Sync to Lidarr (albums).shortcut"
The albums version sends one "Album Artist<TAB>Album" line per song instead of just the artist.
"""
import plistlib, uuid, os

SETUP, SPLIT, URL, TOKEN, FIND, ARTISTS, COMBINED, LINE, REPEAT = (str(uuid.uuid4()).upper() for _ in range(9))
GROUP = str(uuid.uuid4()).upper()


def out(u, name):
    return {"OutputUUID": u, "Type": "ActionOutput", "OutputName": name}


def var(u, name):
    return {"Value": out(u, name), "WFSerializationType": "WFTextTokenAttachment"}


def text(s, attachments=None):
    return {"Value": {"string": s, "attachmentsByRange": attachments or {}}, "WFSerializationType": "WFTextTokenString"}


def song(prop):
    return {"Type": "Variable", "VariableName": "Repeat Item", "Aggrandizements": [
        {"Type": "WFCoercionVariableAggrandizement", "CoercionItemClass": "WFMPMediaItemContentItem"},
        {"Type": "WFPropertyVariableAggrandizement", "PropertyName": prop}]}


setup = [
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
]
artists = [
    # 5: album artist of each song
    {"WFWorkflowActionIdentifier": "is.workflow.actions.properties.music",
     "WFWorkflowActionParameters": {"UUID": ARTISTS, "WFInput": var(FIND, "Music"), "WFContentItemPropertyName": "Album Artist"}},
    # 6: one per line
    {"WFWorkflowActionIdentifier": "is.workflow.actions.text.combine",
     "WFWorkflowActionParameters": {"UUID": COMBINED, "text": var(ARTISTS, "Album Artist"), "WFTextSeparator": "New Lines"}},
]
albums = [
    # 5-7: "Album Artist<TAB>Album" for each song, one per line
    {"WFWorkflowActionIdentifier": "is.workflow.actions.repeat.each",
     "WFWorkflowActionParameters": {"GroupingIdentifier": GROUP, "WFControlFlowMode": 0, "WFInput": var(FIND, "Music")}},
    {"WFWorkflowActionIdentifier": "is.workflow.actions.gettext",
     "WFWorkflowActionParameters": {"UUID": LINE, "WFTextActionText": text("\ufffc\t\ufffc", {"{0, 1}": song("Album Artist"), "{2, 1}": song("Album")})}},
    {"WFWorkflowActionIdentifier": "is.workflow.actions.repeat.each",
     "WFWorkflowActionParameters": {"GroupingIdentifier": GROUP, "WFControlFlowMode": 2, "UUID": REPEAT}},
    {"WFWorkflowActionIdentifier": "is.workflow.actions.text.combine",
     "WFWorkflowActionParameters": {"UUID": COMBINED, "text": var(REPEAT, "Repeat Results"), "WFTextSeparator": "New Lines"}},
]
post = [
    # POST to the URL from the setup code, with its token
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

def shortcut(actions):
    return {
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
for name, middle in (("unsigned.shortcut", artists), ("unsigned-albums.shortcut", albums)):
    with open(os.path.join(here, name), "wb") as f:
        plistlib.dump(shortcut(setup + middle + post), f, fmt=plistlib.FMT_BINARY)
    print("wrote", name)

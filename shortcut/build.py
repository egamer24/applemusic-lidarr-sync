"""Build the unsigned Shortcuts. Sign on a Mac with:
shortcuts sign --mode anyone -i shortcut/unsigned.shortcut -o "shortcut/Sync to Lidarr.shortcut"
shortcuts sign --mode anyone -i shortcut/unsigned-albums.shortcut -o "shortcut/Sync to Lidarr (albums).shortcut"
The albums version sends one "Album Artist<TAB>Album" line per song instead of just the artist.
Songs are read and uploaded in chunks of CHUNK (?part=i&parts=n): Shortcuts fails reading the details of
~2,500+ songs in one go (issue #1). The server only replaces the list once the last part arrives.
"""
import plistlib, uuid, os

CHUNK = 1000  # songs per upload; the reported failure starts around 2,572
(SETUP, SPLIT, URL, TOKEN, FIND, ARTISTS, COMBINED, LINE, REPEAT, COUNT, DIV, PARTS, PARTNO, MUL, START,
 LASTEND, END, ENDNUM, CHUNKED) = (str(uuid.uuid4()).upper() for _ in range(19))
GROUP, LOOP, IFG = (str(uuid.uuid4()).upper() for _ in range(3))


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
    # parts = ceil(songs / CHUNK)
    {"WFWorkflowActionIdentifier": "is.workflow.actions.count",
     "WFWorkflowActionParameters": {"UUID": COUNT, "Input": var(FIND, "Music")}},
    {"WFWorkflowActionIdentifier": "is.workflow.actions.math",
     "WFWorkflowActionParameters": {"UUID": DIV, "WFInput": var(COUNT, "Count"), "WFMathOperation": "÷", "WFMathOperand": CHUNK}},
    {"WFWorkflowActionIdentifier": "is.workflow.actions.round",
     "WFWorkflowActionParameters": {"UUID": PARTS, "WFInput": var(DIV, "Calculation Result"), "WFRoundMode": "Always Round Up"}},
    # for each part: songs (part-1)*CHUNK+1 .. min(part*CHUNK, songs)
    {"WFWorkflowActionIdentifier": "is.workflow.actions.repeat.count",
     "WFWorkflowActionParameters": {"GroupingIdentifier": LOOP, "WFControlFlowMode": 0, "WFRepeatCount": var(PARTS, "Rounded Number")}},
    {"WFWorkflowActionIdentifier": "is.workflow.actions.number",
     "WFWorkflowActionParameters": {"UUID": PARTNO, "WFNumberActionNumber": {"Value": {"Type": "Variable", "VariableName": "Repeat Index"}, "WFSerializationType": "WFTextTokenAttachment"}}},
    {"WFWorkflowActionIdentifier": "is.workflow.actions.math",
     "WFWorkflowActionParameters": {"UUID": MUL, "WFInput": var(PARTNO, "Number"), "WFMathOperation": "×", "WFMathOperand": CHUNK}},
    {"WFWorkflowActionIdentifier": "is.workflow.actions.math",
     "WFWorkflowActionParameters": {"UUID": START, "WFInput": var(MUL, "Calculation Result"), "WFMathOperation": "-", "WFMathOperand": CHUNK - 1}},
    {"WFWorkflowActionIdentifier": "is.workflow.actions.conditional",
     "WFWorkflowActionParameters": {"GroupingIdentifier": IFG, "WFControlFlowMode": 0, "WFCondition": 2,
                                    "WFInput": {"Type": "Variable", "Variable": var(MUL, "Calculation Result")},
                                    "WFNumberValue": var(COUNT, "Count")}},
    {"WFWorkflowActionIdentifier": "is.workflow.actions.number",
     "WFWorkflowActionParameters": {"UUID": LASTEND, "WFNumberActionNumber": var(COUNT, "Count")}},
    {"WFWorkflowActionIdentifier": "is.workflow.actions.conditional",
     "WFWorkflowActionParameters": {"GroupingIdentifier": IFG, "WFControlFlowMode": 1}},
    {"WFWorkflowActionIdentifier": "is.workflow.actions.number",
     "WFWorkflowActionParameters": {"UUID": ENDNUM, "WFNumberActionNumber": var(MUL, "Calculation Result")}},
    {"WFWorkflowActionIdentifier": "is.workflow.actions.conditional",
     "WFWorkflowActionParameters": {"GroupingIdentifier": IFG, "WFControlFlowMode": 2, "UUID": END}},
    {"WFWorkflowActionIdentifier": "is.workflow.actions.getitemfromlist",
     "WFWorkflowActionParameters": {"UUID": CHUNKED, "WFInput": var(FIND, "Music"), "WFItemSpecifier": "Items in Range",
                                    "WFItemRangeStart": var(START, "Calculation Result"), "WFItemRangeEnd": var(END, "If Result")}},
]
artists = [
    # 5: album artist of each song
    {"WFWorkflowActionIdentifier": "is.workflow.actions.properties.music",
     "WFWorkflowActionParameters": {"UUID": ARTISTS, "WFInput": var(CHUNKED, "Items in Range"), "WFContentItemPropertyName": "Album Artist"}},
    # 6: one per line
    {"WFWorkflowActionIdentifier": "is.workflow.actions.text.combine",
     "WFWorkflowActionParameters": {"UUID": COMBINED, "text": var(ARTISTS, "Album Artist"), "WFTextSeparator": "New Lines"}},
]
albums = [
    # 5-7: "Album Artist<TAB>Album" for each song, one per line
    {"WFWorkflowActionIdentifier": "is.workflow.actions.repeat.each",
     "WFWorkflowActionParameters": {"GroupingIdentifier": GROUP, "WFControlFlowMode": 0, "WFInput": var(CHUNKED, "Items in Range")}},
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
         "WFURL": text("\ufffc?part=\ufffc&parts=\ufffc", {"{0, 1}": out(URL, "Item from List"),
                                                       "{7, 1}": out(PARTNO, "Number"), "{15, 1}": out(PARTS, "Rounded Number")}),
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
    {"WFWorkflowActionIdentifier": "is.workflow.actions.repeat.count",
     "WFWorkflowActionParameters": {"GroupingIdentifier": LOOP, "WFControlFlowMode": 2}},
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

# syncsub: transfer timing from a timed subtitle to an OCR / softsub subtitle.
# Based on Sync-timing-subtitles by Luudanmatcuoi (https://github.com/luudanmatcuoi-vn/Sync-timing-subtitles)

import re, argparse, os, sys, json, html, threading, time, webbrowser, unicodedata, socket
import yaml, pysubs2, textdistance
from colour import Color
from pathlib import Path
from urllib.parse import quote, unquote
from flask import Flask, render_template_string, request, jsonify
from pyvi import ViTokenizer, ViPosTagger

# Windows console is not utf-8 by default, printing Vietnamese would crash
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# Config files live next to the .exe when built with pyinstaller --onefile
root_folder = Path(sys.executable if getattr(sys, "frozen", False) else __file__).parent.resolve()

with open(os.path.join(root_folder, "default.yaml" ), encoding="utf-8") as f:
    default_config = yaml.load(f, Loader=yaml.FullLoader)
try:
    with open(os.path.join(root_folder, "recent.yaml" ), encoding="utf-8") as f:
        recent_config = yaml.load(f, Loader=yaml.FullLoader) or {}
except FileNotFoundError:
    recent_config = {}

def pysubs2load(str):
    try:
        return pysubs2.load(str)
    except:
        return pysubs2.load(str, encoding="utf-16")

def get_para_from_recent(default = False):
    if default:
        parameter = dict(default_config)
        parameter.pop('Tooltip', None)
    else:
        parameter = dict(default_config)
        parameter.pop('Tooltip', None)
        temp_para = dict(recent_config)
        for k in parameter.keys():
            if k in temp_para.keys():
                parameter[k] = temp_para[k]
    return parameter

def convert_parameter(save_recent=False):
    global list_characters
    default_par = dict(default_config)
    default_par.pop('Tooltip', None)
    for ta in parameter.keys():
        if ta == "ass_sign_tags":
            continue  # Can be list or __-joined string; skip generic type coercion
        if type(default_par[ta]) == bool and isinstance(parameter[ta], str):
            parameter[ta] = parameter[ta].strip().lower() in ("1", "true", "yes")  # from command line
        else:
            parameter[ta] = type(default_par[ta])(parameter[ta])

    # Check output_filename (empty = next to timed sub, a folder is kept as output folder of season mode)
    if parameter["output_filename"] and not os.path.isdir(parameter["output_filename"]):
        pre, ext = os.path.splitext(parameter["output_filename"])
        parameter["output_filename"] = pre + ".ass"

    list_characters = parameter["characters"]

    if save_recent:
        recent_config = dict(parameter)
        if type(recent_config["ass_sign_tags"]) == type([]):
            recent_config["ass_sign_tags"] = "__".join(recent_config["ass_sign_tags"])
        with open(os.path.join(root_folder, "recent.yaml" ), "w", encoding="utf-8") as f:
            yaml.dump( recent_config, stream=f, default_flow_style=False, sort_keys=False )

    if type(parameter["ass_sign_tags"]) == type(""):
        parameter["ass_sign_tags"] = parameter["ass_sign_tags"].split("__")

    if os.path.isfile(parameter["origin_sub_path"]) and os.path.isfile(parameter["ocr_sub_path"]):
        return True, "Please wait..."
    else:
        return False, "Wrong path, please try again"

parameter = get_para_from_recent()
list_characters = parameter["characters"]

# Break marks for splitting 1 ocr line into many, strongest first ("]" = line break)
split_punctuation = "].!?;,-"
# Colors of lines in split/combine groups of editor
editor_colors = ["green", "blue", "red", "purple", "orange", "yellow"]

def split_line(stri):
    # Split 1 line into 2 at the strongest break mark of split_punctuation
    # A \N before a lowercase word is just wrapping, not a break
    temp_split_line = re.sub(r'^\\N {0,5}([a-z0-9' +re.escape(list_characters.lower())+r"])", r" \1", stri)
    temp_split_line = temp_split_line.replace("\\N", " ] ")
    # Tokens: runs of text, "..." or runs of punctuation
    temp_split_line = re.findall(
        r"[^!\"#$%&\\\'()*+,\-.\/:;<=>?@\[\\\]^_`{|}~]+|\.\.\.|[!\"#$%&\\\'()*+,\-.\/:;<=>?@\[\\\\\]^_`{|}~]+", temp_split_line)

    result = ""
    for ch in split_punctuation:
        if ch in temp_split_line and result=="":
            result = ["".join(temp_split_line[:temp_split_line.index(ch)+1]), \
                    "".join(temp_split_line[temp_split_line.index(ch)+1:])]
            try:
                result.remove("")
            except:
                pass
            if len(result)==1:
                result = ""
    if result == "":
        result = [stri]

    for ia in range(len(result)):
        # Remove break marker and leading dash/dot, capitalize
        result[ia] = result[ia].replace("]", "").lstrip().rstrip()
        result[ia] = re.sub(r"^( ){0,3}[-\.]( ){0,3}","",result[ia])
        if len(result[ia])>0:
            result[ia] = result[ia][0].upper() + result[ia][1:]

    try:
        result.remove("")
    except:
        pass

    return result

def cal_same_time(line1, line2):
    # Overlap of 2 lines in ms
    if line1 is None or line2 is None:
        return 0
    start1, end1 = line1.start, line1.end
    start2, end2 = line2.start, line2.end
    temp = (abs(start1 - end1) + abs(start2 - end2) - abs(start1 - start2) - abs(end1 - end2)) // 2
    if temp < 0:
        return 0
    else:
        return temp

def duration(line):
    if line is None:
        return 0
    return line.end - line.start

def is_same_sub(a, b):
    if (textdistance.hamming.distance(a, b) >= parameter["distance_string_character"] and
            textdistance.hamming.normalized_similarity(a, b) <= parameter["distance_string_rate"]):
        return False
    else:
        return True

def is_same_time(a, b):
    if a is None or b is None:
        return False
    if abs(a.start - b.start) >= parameter["time_tolerance"] or abs(a.end - b.end) >= parameter["time_tolerance"]:
        return False
    else:
        return True

def is_continue_time(a, b ):
    if min(abs(a.end-b.start),abs(a.start-b.end)) < parameter["time_tolerance"]:
        return True
    else:
        return False

def clean_inline_comment(stri):
    # Remove {comments}, keep {\tags}
    stri = stri.replace("\\N","\x00")
    stri = re.sub(r"\{[^\\\{\}]+\}","",stri)
    return stri.replace("\x00","\\N")

def flat_line(l):
    # Key to compare 2 lines
    if l is None:
        return ""
    return f"l_{str(l.layer)}_s_{str(l.start)}_e_{str(l.end)}_c_{str(l.is_comment)}_s_{str(l.style)}_n_{str(l.name)}_t_{str(l.text)}"

def is_uppercase(stri):
    stri = stri.replace("-","").strip()
    if stri=="": return False
    if stri[0]==stri[0].lower(): return False
    return True

def gen_color(stt, lum=None, sat = None):
    if type(stt)==type(1):
        c = Color(editor_colors[stt%len(editor_colors)])
    else:
        c = Color(stt)
    if lum!=None:
        c.luminance = lum
    if sat!=None:
        c.saturation = sat
    return c.hex_l

def generate_graph_image(group, listgroup, lum=None, sat=None):
    # Bars of the timing graph shown in editor for split_sub groups
    # Time is normalized to 0-100 over all lines of listgroup
    start = min([g["start"] for t in listgroup for g in t])
    end = max([g["end"] for t in listgroup for g in t])
    le = abs(end-start)
    if le == 0:
        le = 1
    # Normalize and assign colors
    graph = group.copy()
    for g in range(len(graph)):
        graph[g]["start_nom"] = int((graph[g]["start"] - start)/le*100//1)
        graph[g]["end_nom"] = int((graph[g]["end"] - start)/le*100//1)
        graph[g]["color"] = gen_color(g, lum, sat)
        graph[g]["level"] = 0
    # Calculate levels to avoid overlaps
    for g in range(len(graph)):
        while True:
            same_level = [graph[ga] for ga in range(g) if graph[ga]["level"] == graph[g]["level"]]
            if any([(abs(sa["start"]-sa["end"]) + abs(graph[g]["start"]-graph[g]["end"]) -
                   abs(sa["start"]-graph[g]["start"]) - abs(sa["end"]-graph[g]["end"])) > 2 for sa in same_level]):
                graph[g]["level"] += 1
            else:
                break
    try:
        max_level = max([g["level"] for g in graph])
    except Exception:
        max_level = 0

    rects = []
    for g in graph:
        rects.append({
            "level": g["level"],
            "top": g["start_nom"],
            "height": g["end_nom"] - g["start_nom"],
            "color": g["color"]
        })
    return {"rects": rects, "max_level": max_level}


def clean_html_from_sub(text):
    if not isinstance(text, str):
        return text
    text = re.sub(r'<\s*br\s*/?>', '\n', text, flags=re.I)
    text = re.sub(r'<\s*/?\s*(?:div|p)\s*[^>]*>', '\n', text, flags=re.I)
    text = re.sub(r'<[^>]+>', '', text)
    text = html.unescape(text).replace('\xa0', ' ')
    lines = [l.strip(' \t\r') for l in text.split('\n')]
    return '\n'.join([l for l in lines if l != ''])

def apply_sub(a_line, text):
    # Put new text into timed line a_line: keep its {\tags}, turn its old text into {comment}
    # text can be a string, dict from editor, SSAEvent or [SSAEvent]
    if type(a_line) == type([]) and len(a_line) == 1:
        a_line = a_line[0]
    if type(text) == type({}):
        text = text["text"]
    elif type(text) != type("") and type(text) != type([]):
        text = text.text
    elif type(text) == type([]) and len(text) == 1:
        text = text[0].text
    text = clean_html_from_sub(text)
    text = text.replace("\n","\\N")
    text = text.replace("\\n","\\N")
    # Old {comments} become plain text, only {\tags} stay in braces
    temp = a_line.text.replace("{","[").replace("}","]")
    temp = re.sub(r"\[(\\[^\]]+)\]", r"{\1}", temp)

    match = re.findall(r"({[^{}]+})?([^{}]+)?", temp)
    temp = ""
    for t in match:
        for m in t:
            if m == "":
                pass
            # Tag -> keep
            elif "{" in m:
                temp += m
            # Old text -> {comment} (or drop it)
            elif "{" not in m:
                if parameter["keep_timed_text"]:
                    temp += "{" + m + "}"
    text = temp + text
    # Remove repeated comments
    text = re.sub(r"(\{[^\{\}]+\}({[^\{\}]+\})*)\1{1,}",r"\1",text)
    a_line.text = text
    return a_line

def split_vietnamese_sentence(text):
    # Wrap a long line into 2 with \N, at a natural break between 40%-60% of its length
    if "\\N" in text or "\n" in text: return text
    tokens = ViTokenizer.tokenize(text).split()
    pos_tags = ViPosTagger.postagging(ViTokenizer.tokenize(text))[1]
    tokenl = [len(to) for to in tokens]
    textl = len(text)

    def findratio(tokenl, ratio, over=False):
        for t in range(len(tokenl)):
            if (sum(tokenl[:t])+t)/textl>=ratio:
                if over:
                    return t
                else:
                    return t-1
        return len(tokenl)

    # 1 = break exactly at the middle
    def calrate(i):
        tem1 = sum(tokenl[:i]) + i
        tem2 = sum(tokenl[i:]) + len(tokens) - i
        tem3 = 1-min(tem1,tem2)/(textl/2)
        return tem3

    i_min = findratio(tokenl,0.4)
    i_max = findratio(tokenl,0.6)

    # Score each break position by POS tag (pyvi), prefer breaking near the middle
    pos_split = []
    for i in range(i_min,i_max):
        pos = pos_tags[i]
        pos2 = pos_tags[i+1]
        # After punctuation
        if pos[0] in ["F"] and pos2[0] not in ["F"]:
            pos_split+=[[i, 3+calrate(i),pos, tokens[i] ]]
        # After adjective / preposition
        elif pos[0] in ["A","E"]:
            pos_split+=[[i, 1+calrate(i),pos, tokens[i] ]]
        # Before conjunction / preposition
        elif pos2[0] in ["C","E"]:
            pos_split+=[[i, 1+calrate(i),pos, tokens[i] ]]
        # After a verb phrase
        elif pos[0] in ["V"] and pos2[0] not in ["V"]:
            pos_split+=[[i, 1+calrate(i),pos, tokens[i] ]]
        else:
            pos_split+=[[i, calrate(i),pos, tokens[i] ]]

    # No place to split (eg. 1 very long word)
    if len(pos_split) == 0:
        return text
    pos_split = max(pos_split, key=lambda x: x[1] )[0]
    if pos_split+1 >= len(tokens):
        return text
    pos_index = sum(tokenl[: pos_split ])
    tokens = tokens[pos_split+1].replace("_"," ")
    text = text[:pos_index] + text[pos_index:].replace( tokens, "\\N"+tokens, 1)
    return text

def combine_sub(a_group, b_group):
    # 1 timed line - many ocr lines: join ocr lines into 1
    # Merge repeated ocr lines that follow each other (same text shown twice)
    i = 0
    while i+1 < len(b_group):
        if b_group[i + 1] is None: break
        if is_same_sub(b_group[i].text, b_group[i + 1].text) and is_continue_time(b_group[i], b_group[i + 1]):
            b_group[i].end = b_group[i + 1].end
            del b_group[i + 1]
        else:
            i += 1
    if "\\N" in a_group[0].text and len(b_group) > 1:
        # Timed line has 2 lines and the 2nd starts uppercase (2 speakers) -> join with \N
        if is_uppercase(a_group[0].text.split("\\N")[1]):
            def clear_newline(stri):
                stri = re.sub(r"( )*\n( )*"," ",stri)
                stri = re.sub(r"( )*\\N( )*"," ",stri)
                return stri
            b_group[0].text = "\\N".join([clear_newline(t.plaintext) for t in b_group])
            b_group = [b_group[0]]
        # 2nd line starts lowercase (same sentence) -> join with space
        else:
            b_group[0].text = " ".join([t.plaintext.strip("\n\r ") for t in b_group])
            b_group = [b_group[0]]
    elif "\\N" not in a_group[0].text and len(b_group) > 1:
        b_group[0].text = " ".join([t.plaintext.strip("\n\r ") for t in b_group])
        b_group = [b_group[0]]

    # Wrap if too long
    if len(b_group[0].text) > parameter["max_line_length"] and "\\N" not in b_group[0].text:
        b_group[0].text = split_vietnamese_sentence(b_group[0].text)
    return b_group

def split_sub(a_group, b_group):
    # Many timed lines - fewer ocr lines: split ocr text at punctuation until counts match
    # Returns (timed lines, split ocr lines, timing graph)
    def count_split_symbol(stri):
        return sum([stri.count(gt) for gt in split_punctuation])
    def split_line_dict(sow):
        kwow = split_line(sow["text"])
        return [{"text":kwows,"splited":True,
                 "start": sow["start"], "end": sow["end"],
                 "count":count_split_symbol(kwows)} for kwows in kwow]

    split = [{"text":clean_inline_comment(b_group[t].text), "splited":False,
             "count":count_split_symbol(b_group[t].text),
             "start": b_group[t].start,
             "end": b_group[t].end  } for t in range(len(b_group))]

    # Split the line with most punctuation first, each line only once
    while len(split) < len(a_group):
        need_split = [s for s in split if not s["splited"]]
        if len(need_split)>0:
            need_split = max(need_split, key = lambda p : p["count"])
            need_split = split.index(need_split)
            split = split[:need_split] + split_line_dict(split[need_split]) + split[need_split+1:]
        else:
            break

    # Remove phrases already in a previous piece
    tempsplit = [t["text"].split("\\N") for t in split]
    for g in range(len(tempsplit)):
        before = [bla for qa in tempsplit[:g] for bla in qa]
        for q in list(tempsplit[g]):
            if q in before and len(tempsplit[g])>1:
                tempsplit[g].remove(q)
    tempsplit = ["\\N".join(t) for t in tempsplit]
    for g in range(len(tempsplit)):
        split[g]["text"] = tempsplit[g]
    # Pad with empty lines if still fewer than timed lines
    try:
        b_group = split + [{"text":"", "splited":True, "count":0, "start": split[-1]["end"],  "end": split[-1]["end"]  }] * ( len(a_group) - len(split) )
    except:
        b_group = split + [{"text":"", "splited":True, "count":0, "start": 0,  "end": 1  }] * ( len(a_group) - len(split) )

    for q in range(len(a_group)):
        a_group[q].name = a_group[q].name+"__tag:split"

    temp_graph = [[{"text":ga.text,"start":ga.start,"end":ga.end} for ga in a_group],b_group]
    graph1 = generate_graph_image( [{"text":ga.text,"start":ga.start,"end":ga.end} for ga in a_group], temp_graph, lum=3/5 )
    graph2 = generate_graph_image( b_group, temp_graph,lum=2/5,sat=2/3)
    bo=[]
    for b in b_group:
        bo+=[ pysubs2.SSAEvent(start=pysubs2.make_time(ms=b["start"]), end=pysubs2.make_time(ms=b["end"]), text=b["text"]) ]

    return a_group, bo, [graph1, graph2]

def convert_actor(va):
    # The Actor field carries internal data while processing: "id:12__oldname:Bob__tag:sign" <-> dict
    if isinstance(va, str):
        res = {}
        va = va.split("__")
        for v in va:
            key = v[:v.index(":")]
            cla = v[v.index(":")+1:]
            if key == "oldname":
                cla = unquote(cla)
            elif key == "id":
                cla = float(cla)
                if cla%1 ==0:
                    cla = int(cla)
            res[key] = cla
        return res
    elif isinstance(va, dict):
        # Original name is %-encoded so names containing "__" or ":" survive
        res = []
        for d in va.keys():
            v = str(va[d])
            if d == "oldname":
                v = quote(v, safe="").replace("_", "%5F")
            res+=[d+":"+v]
        res = "__".join(res)
        return res

# ---------------------------------------- Processing ----------------------------------------

# State shared between prepare() (before editor) and finalize() (after editor submit)
state = {}

def prepare():
    # Load, filter and group lines of 2 subs into state["web_groups"] for the editor
    eng_sub = pysubs2load(parameter["origin_sub_path"])
    ocr_sub = pysubs2load(parameter["ocr_sub_path"])
    if parameter["custom_shift"] != 0:
        ocr_sub.shift(ms=parameter["custom_shift"])

    # Number timed lines to restore their order at export
    for i_s in range(len(eng_sub)):
        old_name = eng_sub[i_s].name
        eng_sub[i_s].name = convert_actor( {"oldname":old_name, "id":str(i_s+1)} )

    # Take sign lines out of timed sub
    eng_signs = []
    i_eng = 0
    while i_eng < len(eng_sub):
        if any(["\\" + tag + "(" in eng_sub[i_eng].text for tag in parameter["ass_sign_tags"] ]) and not eng_sub[
            i_eng].is_comment:
            eng_sub[i_eng].name = eng_sub[i_eng].name+"__tag:sign"
            eng_signs += [eng_sub[i_eng]]
            del eng_sub[i_eng]
        else:
            i_eng += 1

    # Take comment lines out of timed sub
    comment_group = []
    i_eng = 0
    while i_eng < len(eng_sub):
        if eng_sub[i_eng].is_comment:
            eng_sub[i_eng].name = eng_sub[i_eng].name+"__tag:comment"
            comment_group += [eng_sub[i_eng]]
            del eng_sub[i_eng]
        else:
            i_eng += 1

    # Most used layer, given to lines that only exist in ocr sub
    layer_eng_sub = [eng.layer for eng in eng_sub if eng.layer!=0]
    if len(layer_eng_sub)>0:
        layer_eng_sub = max(set(layer_eng_sub), key=layer_eng_sub.count)
    else:
        layer_eng_sub = 0

    # Rules: type________find________replace, lines starting with # are skipped
    try:
        f = open(os.path.join(root_folder, "filter_rules.txt" ), "r", encoding="utf-8")
        rules = [g.replace("\\n", "\\\\N") for g in f.read().splitlines() if len(g) > 0]
        rules = [g.split("________") for g in rules if "#" not in g[0]]
        f.close()
    except:
        rules = []

    # If > 20% of ocr text is not in "characters", the setting is wrong: only warn, don't remove lines
    tempa = "".join([t.text for t in ocr_sub ]).replace(" ","").replace("\\N","").replace("\n","")
    tempb = tempa
    tempa = re.sub(  r"[^0-9a-zA-Z\s\W%s]" % re.escape(list_characters) , "", tempa)
    if len(tempa) < len(tempb) * (1 - 20 / 100):
        print("Please fill all characters of your language in 'characters' default.yaml\n(without A-Z a-z 0-9 or punctuation)")
        is_remove_lines_unexpected = False
    else:
        is_remove_lines_unexpected = True

    i_ocr = 0
    ocr_signs = []
    while i_ocr < len(ocr_sub):
        if ocr_sub[i_ocr].is_comment:
            del ocr_sub[i_ocr]
            continue
        if any(["\\" + tag + "(" in ocr_sub[i_ocr].text for tag in parameter["ass_sign_tags"]]):
            ocr_signs += [ocr_sub[i_ocr]]
            del ocr_sub[i_ocr]
            continue
        # Fix decomposed unicode (e + ◌́ -> é)
        ocr_sub[i_ocr].text = unicodedata.normalize("NFC", ocr_sub[i_ocr].text)
        for r in rules:
            if r[0] == "replace":
                if "\\u" in r[1]:
                    abcds = re.findall(r"\\u[0-9ABCDEFabcdef]{2,4}", r[1])
                    for abcd in abcds:
                        r[1] = r[1].replace(abcd, chr(int(abcd[2:], 16)))
                if "\\u" in r[2]:
                    abcds = re.findall(r"\\u[0-9ABCDEFabcdef]{2,4}", r[2])
                    for abcd in abcds:
                        r[2] = r[2].replace(abcd, chr(int(abcd[2:], 16)))
                ocr_sub[i_ocr].text = ocr_sub[i_ocr].text.replace(r[1], r[2])
            elif r[0] == "replace_word":
                ocr_sub[i_ocr].text = re.sub(r"\b" + re.escape(r[1]) + r"\b", lambda m: r[2], ocr_sub[i_ocr].text)
            else:
                # regex, \U...\E and \L...\E change case like in Perl
                regex_str = r[2].replace("\\U","[UPPER]").replace("\\L","[LOWER]").replace("\\E","[END_UL]")
                ocr_sub[i_ocr].text = re.sub(r[1], regex_str, ocr_sub[i_ocr].text)
                if "[END_UL]" in ocr_sub[i_ocr].text:
                    ocr_sub[i_ocr].text = re.sub(r"\[UPPER\]([^\[]*)\[END_UL\]", lambda m: f"{m.group(1).upper()}", ocr_sub[i_ocr].text)
                    ocr_sub[i_ocr].text = re.sub(r"\[LOWER\]([^\[]*)\[END_UL\]", lambda m: f"{m.group(1).lower()}", ocr_sub[i_ocr].text)

        match = re.findall(
            r"[^0-9a-zA-Z\s\W%s]" % re.escape(list_characters),
            ocr_sub[i_ocr].text)
        if len(match) > 0 and "OCR_EMPTY_RESULT" not in ocr_sub[i_ocr].text and is_remove_lines_unexpected :
            print("Caution unexpected characters:\t" + " ".join(match) + "\t" + ocr_sub[i_ocr].text)

        # Remove rows (\N) with > 60% unexpected characters, then the line if nothing is left
        tempa = ocr_sub[i_ocr].text.split("\\N")
        tempb = list(tempa)
        for t in range(len(tempa)):
            tempa[t] = re.sub(  r"[^0-9a-zA-Z\s\W%s]" % re.escape(list_characters) , "", tempa[t])
            ga = tempa[t].replace(" ", "")
            gb = tempb[t].replace(" ", "")
            if len(ga) < len(gb) * (1 - 60 / 100) and is_remove_lines_unexpected:
                tempb[t] = ""
        tempb = list(filter(None, tempb))
        if len(tempb) == 0:
            del ocr_sub[i_ocr]
            continue
        ocr_sub[i_ocr].text = "\\N".join(tempb)

        # Placeholder of OCR tools for an empty image
        if "OCR_EMPTY_RESULT" in ocr_sub[i_ocr].text:
            del ocr_sub[i_ocr]
            continue
        else:
            i_ocr += 1

    # An ocr line spanning 2 continuous timed lines almost equally is stretched to cover both
    temp_eng_time = [t.copy() for t in eng_sub if t.is_comment is False]
    temp_eng_time = sorted(temp_eng_time, key=lambda t: t.start)
    for i in range(len(ocr_sub)):
        for t in range(len(temp_eng_time) - 1):
            if cal_same_time(temp_eng_time[t + 1], ocr_sub[i]) ==0:
                continue
            if is_continue_time(temp_eng_time[t], temp_eng_time[t + 1]) \
                    and ocr_sub[i].start < temp_eng_time[t].end < ocr_sub[i].end \
                    and 1 - abs(
                1 - cal_same_time(temp_eng_time[t], ocr_sub[i]) / cal_same_time(temp_eng_time[t + 1], ocr_sub[i])) > \
                    parameter["soft_combine_rate"]:
                if ocr_sub[i].start > temp_eng_time[t].start:
                    ocr_sub[i].start = temp_eng_time[t].start
                if ocr_sub[i].end < temp_eng_time[t + 1].end:
                    ocr_sub[i].end = temp_eng_time[t + 1].end

    # Pair every ocr line with timed lines overlapping > same_rate, unpaired lines stand alone
    group = []
    for ocr_line in ocr_sub:
        is_has_engsub_group = False
        for eng_line in eng_sub:
            same_time = cal_same_time(eng_line, ocr_line)
            if same_time > duration(eng_line)*parameter["same_rate"] or same_time > duration(ocr_line)*parameter["same_rate"]:
                group += [{"start": eng_line.start, "end": eng_line.end, "eng": eng_line, "ocr": ocr_line}]
                is_has_engsub_group = True
        if not is_has_engsub_group:
            group += [{"start": ocr_line.start, "end": ocr_line.end, "eng": None, "ocr": ocr_line}]

    for eng_line in eng_sub:
        is_has_engsub_group = False
        for ocr_line in ocr_sub:
            same_time = cal_same_time(ocr_line, eng_line)
            if same_time > duration(ocr_line) * parameter["same_rate"] or same_time > duration(eng_line) * parameter[
                "same_rate"]:
                is_has_engsub_group = True
        if not is_has_engsub_group:
            group += [{"start": eng_line.start, "end": eng_line.end, "eng": eng_line, "ocr": None}]

    group = sorted(group, key=lambda d: d['start'])

    # Merge pairs sharing a line or time into groups, then classify each group
    i_group = 0
    web_groups = []
    while i_group < len(group):
        a = []
        b = []
        a += [group[i_group]["eng"]]
        b += [group[i_group]["ocr"]]

        # Take following pairs while they share a line or time with this group
        following = 1
        real_following = 1
        while True:
            temp_i = i_group + following

            if temp_i == len(group):
                break

            is_group_add = False
            for gia in range(len(a)):
                if flat_line(group[temp_i]["eng"]) == flat_line(a[gia]) and flat_line(group[temp_i]["ocr"]) != flat_line(b[gia]):
                    is_group_add = True

                if flat_line(group[temp_i]["eng"]) != flat_line(a[gia]) and flat_line(group[temp_i]["ocr"]) == flat_line(b[gia]) :
                    is_group_add = True

                if is_same_time(a[gia],group[temp_i]["eng"]):
                    is_group_add = True

                if is_same_time(b[gia],group[temp_i]["ocr"]):
                    is_group_add = True

                if is_same_time(group[i_group]["eng"],group[temp_i]["eng"]):
                    is_group_add = True

                if cal_same_time(group[temp_i]["ocr"],b[gia]) > duration(group[temp_i]["ocr"])*parameter["same_rate"]:
                    is_group_add = True

                if cal_same_time(group[temp_i]["ocr"],b[gia]) > duration(b[gia])*parameter["same_rate"]:
                    is_group_add = True
            if is_group_add:
                a += [group[temp_i]["eng"]]
                b += [group[temp_i]["ocr"]]
                real_following = following +1
            else:
                break

            following += 1

        # Remove None and duplicate lines
        g = 0
        while g<len(a):
            if a[g] is None:
                del a[g]
            elif flat_line(a[g]) in [flat_line(ka) for ka in a[:g]]:
                del a[g]
            else:
                g+=1
        g = 0
        while g<len(b):
            if b[g] is None:
                del b[g]
            elif flat_line(b[g]) in [flat_line(ka) for ka in b[:g]]:
                del b[g]
            else:
                g+=1
        i_group += real_following

        # Group type by count of timed (a) - ocr (b) lines: 0-n, n-0, 1-1, 1-n, n-m
        web_group = {"web_id":len(web_groups), "type": None, "group_a":a, "group_b":b, "graphic":[] }
        if len(a) == 0:
            web_group["type"] = "from_ocr"

        elif len(b) == 0:
            web_group["type"] = "from_timed"
            web_group["group_b"] = [pysubs2.SSAEvent( start=0 , end=0, text="") for w in web_group["group_a"] ]

        elif len(a) == 1 and len(b) == 1:
            if len(b[0].text) > parameter["max_line_length"]:
                web_group["group_b"][0].text = split_vietnamese_sentence( web_group["group_b"][0].text )
            web_group["type"] = ""

        elif len(a) == 1 and len(b) > 1:
            web_group["type"] = "combine_sub"
            web_group["group_b"] = combine_sub(a, b)

        elif len(a) > 1:
            web_group["type"] = "split_sub"
            a, b, graph = split_sub(a, b)
            web_group["group_a"] = a
            web_group["group_b"] = b
            web_group["graphic"] = graph

        web_groups+=[web_group]

    state.clear()
    state.update({"web_groups": web_groups, "eng_signs": eng_signs, "ocr_signs": ocr_signs,
                  "comment_group": comment_group, "layer_eng_sub": layer_eng_sub})

def finalize(web_return):
    # Apply edited texts from editor to timed lines, add signs and comments back, save .ass
    web_groups = state["web_groups"]
    layer_eng_sub = state["layer_eng_sub"]
    best_subtitle = []
    for w in range(len(web_return)):
        a = web_groups[w]["group_a"]
        b = web_return[w]["group_b"]

        for bi in range(len(b)):
            b[bi]["text"] = clean_html_from_sub(b[bi]["text"])

        if web_groups[w]["type"]=="from_ocr":
            # Only in ocr sub -> comment line, placed after previous line
            for i in range(len(b)):
                try:
                    ids = convert_actor(best_subtitle[-1].name)["id"]
                except:
                    ids = 0
                b[i] = pysubs2.SSAEvent(
                    start=b[i]["start_ms"],
                    end=b[i]["start_ms"],
                    text = b[i]["text"],
                    name = convert_actor({"tag":"from_ocr","oldname":"","id":ids}),
                    layer = layer_eng_sub,
                    type = "Comment",
                    )
            best_subtitle += b

        elif web_groups[w]["type"]=="from_timed":
            for i_s in range(len(a)):
                a[i_s].name = a[i_s].name+"__tag:from_timed"
            if parameter["comment_timed_sub"]:
                best_subtitle += [apply_sub(tg, "") for tg in a]
            else:
                best_subtitle += a

        elif web_groups[w]["type"]=="":
            for ai in range(len(a)):
                a[ai].name = a[ai].name+"__tag:"
            for i_s in range(len(a)):
                best_subtitle += [apply_sub(a[i_s], b[i_s])]

        elif web_groups[w]["type"]=="combine_sub":
            for i_s in range(len(a)):
                a[i_s].name = a[i_s].name+"__tag:combine_sub"
                best_subtitle += [apply_sub(a[i_s], b[i_s])]

        elif web_groups[w]["type"]=="split_sub":
            for i_s in range(len(a)):
                a[i_s].name = a[i_s].name+"__tag:split_sub"
                best_subtitle += [apply_sub(a[i_s], b[i_s])]

    # A lone from_timed line next to a lone from_ocr line (between normal lines) is the same line: merge them
    i_s = 0
    while True:
        try:
            if "tag:from_" not in best_subtitle[i_s].name \
            and "tag:from_timed" in best_subtitle[i_s+1].name \
            and "tag:from_ocr" in best_subtitle[i_s+2].name \
            and "tag:from_" not in best_subtitle[i_s+3].name:
                best_subtitle[i_s+1] = apply_sub(best_subtitle[i_s+1], best_subtitle[i_s+2])
                best_subtitle[i_s+1].text = best_subtitle[i_s+1].text.replace("{[","{").replace("]}","}")
                best_subtitle[i_s+1].name = best_subtitle[i_s+1].name.replace("tag:from_timed","tag:")
                del best_subtitle[i_s+2]
            elif "tag:from_" not in best_subtitle[i_s].name \
            and "tag:from_ocr" in best_subtitle[i_s+1].name \
            and "tag:from_timed" in best_subtitle[i_s+2].name \
            and "tag:from_" not in best_subtitle[i_s+3].name:
                best_subtitle[i_s+2] = apply_sub(best_subtitle[i_s+2], best_subtitle[i_s+1])
                best_subtitle[i_s+2].text = best_subtitle[i_s+2].text.replace("{[","{").replace("]}","}")
                best_subtitle[i_s+2].name = best_subtitle[i_s+2].name.replace("tag:from_timed","tag:")
                del best_subtitle[i_s+1]
            else:
                i_s+=1
        except:
            break

    # Signs
    eng_signs = state["eng_signs"]
    ocr_signs = state["ocr_signs"]
    if parameter["is_use_sign_timed"] and len(eng_signs)>0:
        for sign in eng_signs:
            if parameter["is_cmt_sign_timed"]:
                sign.type = "Comment"
            best_subtitle += [sign]

    if parameter["is_use_sign_ocr"] and len(ocr_signs)>0:
        # Ocr signs go to the end, always commented
        last_best_subtitle_id = max([convert_actor(ga.name)["id"] for ga in best_subtitle], default=0)
        step_id = 2
        for ocr_sign in ocr_signs:
            ac = {"id":last_best_subtitle_id+step_id/10000, "tag":"sign","oldname":str(ocr_sign.name)}
            step_id+=1
            ocr_sign.name=convert_actor(ac)
            ocr_sign.type = "Comment"
            best_subtitle+=[ocr_sign]

    for comment in state["comment_group"]:
        best_subtitle += [comment]

    # Back to the original order of timed sub
    best_subtitle = sorted(best_subtitle, key= lambda tq: (convert_actor(tq.name)["id"], tq.start, tq.end ), reverse=False)

    # Restore Actor field: original name + group type tag (only original name if the line has no new text)
    for sub in range(len(best_subtitle)):
        ob = convert_actor(best_subtitle[sub].name)
        if re.match(r"^\{[^\{\}\\]+\}$",best_subtitle[sub].text) is not None:
            ob = [str(ob["oldname"])]
            best_subtitle[sub].text = best_subtitle[sub].text.replace("{[","{").replace("]}","}")
        else:
            ob = [str(ob["oldname"]), str(ob["tag"])]

        ob = [o for o in ob if o not in ["comment",""]]
        if len(ob)==2 and "sign" in ob:
            ob.remove("sign")
        ob = "-".join(ob)
        best_subtitle[sub].name = ob

    # Export with header and styles of timed sub (+ styles of ocr sub if it is .ass)
    write_sub = pysubs2load(parameter["origin_sub_path"])
    while len(write_sub) > 0:
        del write_sub[0]
    if parameter["ocr_sub_path"][-3:] in ["ass", "ssa"]:
        style_file = pysubs2load(parameter["ocr_sub_path"])
        write_sub.import_styles(style_file)

    for b in best_subtitle:
        write_sub.append(b)

    write_sub.save(output_path())
    state.clear()
    print("Saved:", os.path.abspath(output_path()))
    return os.path.abspath(output_path())

# ---------------------------------------- Season mode / draft ----------------------------------------

sub_extensions = (".ass", ".ssa", ".srt", ".vtt", ".sub", ".txt")
# Season mode: queue of (timed sub, ocr sub, output) processed one by one
batch = {"queue": [], "index": 0, "form": {}}

def episode_number(filename):
    name = os.path.splitext(filename)[0]
    m = re.search(r"[Ss]\d+[Ee](\d+)", name)
    if m:
        return int(m.group(1))
    # Drop resolution, codec, bit depth, CRC, version (v2)... before taking the last number
    name = re.sub(r"\d{3,4}p|[xXhH]\.?26[45]|\d+-?bit|\[[0-9A-Fa-f]{8}\]|(?<![A-Za-z])[vV]\d+\b", " ", name)
    numbers = re.findall(r"(?<!\d)(\d{1,3})(?!\d)", name)
    return int(numbers[-1]) if numbers else None

def pair_episodes(origin_folder, ocr_folder, output_folder):
    def list_subs(folder):
        res = {}
        for f in sorted(os.listdir(folder)):
            ep = episode_number(f)
            # Skip outputs of previous runs
            if f.lower().endswith(sub_extensions) and not f.lower().endswith(".synced.ass") and ep is not None and ep not in res:
                res[ep] = os.path.join(folder, f)
        return res
    origin_subs = list_subs(origin_folder)
    ocr_subs = list_subs(ocr_folder)
    queue = []
    for ep in sorted(origin_subs):
        if ep in ocr_subs:
            stem = os.path.splitext(os.path.basename(origin_subs[ep]))[0]
            queue += [(origin_subs[ep], ocr_subs[ep], os.path.join(output_folder, stem + ".synced.ass"))]
    return queue

def load_batch_item():
    parameter["origin_sub_path"], parameter["ocr_sub_path"], parameter["output_filename"] = batch["queue"][batch["index"]]

def output_path():
    # Empty output = next to timed sub
    return parameter["output_filename"] or os.path.splitext(parameter["origin_sub_path"])[0] + ".synced.ass"

def draft_path():
    return output_path() + ".draft.json"

# ---------------------------------------- Web UI ----------------------------------------

app = Flask(__name__)

@app.route('/')
def config_ui():
    return render_template_string("""
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>syncsub</title>
    <style>
        body { font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; background-color: #121212; color: #e0e0e0; margin: 0; padding: 20px; }
        .container { max-width: 800px; margin: auto; background-color: #1e1e1e; padding: 30px; border-radius: 8px; box-shadow: 0 4px 6px rgba(0,0,0,0.3); }
        h1 { text-align: center; color: #ffffff; }
        .form-group { margin-bottom: 15px; }
        label { display: block; font-weight: bold; margin-bottom: 5px; color: #bbbbbb; }
        input[type="text"] { width: 100%; padding: 10px; background-color: #2c2c2c; border: 1px solid #444; color: #fff; border-radius: 4px; box-sizing: border-box; }
        input[type="checkbox"] { margin-right: 10px; }
        .checkbox-group { display: flex; align-items: center; gap: 20px; margin-bottom: 10px; }
        .columns { display: flex; gap: 20px; }
        .path-row { display: flex; gap: 6px; }
        .path-row input { flex: 1; min-width: 0; }
        .browse { padding: 0 12px; background-color: #3a3a3a; color: #e0e0e0; border: 1px solid #555; border-radius: 4px; cursor: pointer; }
        .browse:hover { background-color: #4a4a4a; }
        .column { flex: 1; }
        .btn { display: block; width: 100%; padding: 15px; background-color: #007bff; color: white; border: none; border-radius: 4px; font-size: 16px; cursor: pointer; transition: background 0.3s; }
        .btn:hover { background-color: #0056b3; }
        #loading { display: none; text-align: center; margin-top: 20px; color: #007bff; font-weight: bold; }
        .sliders input { width: 100%; }
        .slider-val { font-weight: bold; color: #007bff; }
    </style>
</head>
<body>
    <div class="container">
        <h1>syncsub</h1>
        <form id="configForm">
            <div class="columns">
                <div class="column">
                    <h3>Timed Files</h3>
                    <div class="form-group">
                        <label title="{{ tip.origin_sub_path }}">Timed sub</label>
                        <div class="path-row">
                            <input type="text" id="origin_sub_path" value="{{ p.origin_sub_path }}">
                            <button type="button" class="browse" onclick="browse('origin_sub_path', 'file')">File</button>
                            <button type="button" class="browse" onclick="browse('origin_sub_path', 'folder')">Folder</button>
                        </div>
                    </div>
                </div>
                <div class="column">
                    <h3>OCR Files</h3>
                    <div class="form-group">
                        <label title="{{ tip.ocr_sub_path }}">OCR sub</label>
                        <div class="path-row">
                            <input type="text" id="ocr_sub_path" value="{{ p.ocr_sub_path }}">
                            <button type="button" class="browse" onclick="browse('ocr_sub_path', 'file')">File</button>
                            <button type="button" class="browse" onclick="browse('ocr_sub_path', 'folder')">Folder</button>
                        </div>
                    </div>
                </div>
            </div>

            <div class="form-group" style="margin-top: 20px;">
                <label title="{{ tip.output_filename }}">Output Filename (empty = next to timed sub)</label>
                <div class="path-row">
                    <input type="text" id="output_filename" value="{{ p.output_filename }}">
                    <button type="button" class="browse" onclick="browse('output_filename', 'save')">Save as</button>
                    <button type="button" class="browse" onclick="browse('output_filename', 'folder')">Folder</button>
                </div>
            </div>

            <h3>For Dialogue:</h3>
            <div class="checkbox-group">
                <label title="{{ tip.comment_timed_sub }}"><input type="checkbox" id="comment_timed_sub" {{ 'checked' if p.comment_timed_sub else '' }}> Comment lines that only appear in timed sub</label>
            </div>

            <h3>For Sign:</h3>
            <div class="checkbox-group">
                <label title="{{ tip.is_use_sign_timed }}"><input type="checkbox" id="is_use_sign_timed" {{ 'checked' if p.is_use_sign_timed else '' }}> Use timed sub</label>
                <label title="{{ tip.is_cmt_sign_timed }}"><input type="checkbox" id="is_cmt_sign_timed" {{ 'checked' if p.is_cmt_sign_timed else '' }}> comment it</label>
            </div>
            <div class="checkbox-group">
                <label title="{{ tip.is_use_sign_ocr }}"><input type="checkbox" id="is_use_sign_ocr" {{ 'checked' if p.is_use_sign_ocr else '' }}> Use OCR sub (commented)</label>
            </div>

            <h3>Additional Parameters</h3>
            <div class="sliders">
                <div class="form-group">
                    <label title="{{ tip.same_rate }}">Same Rate: <span id="val_same_rate" class="slider-val">{{ p.same_rate }}</span></label>
                    <input type="range" id="same_rate" min="0" max="1" step="0.01" value="{{ p.same_rate }}" oninput="document.getElementById('val_same_rate').innerText=this.value">
                </div>
                <div class="form-group">
                    <label title="{{ tip.time_tolerance }}">Time Tolerance (ms): <span id="val_time_tolerance" class="slider-val">{{ p.time_tolerance }}</span></label>
                    <input type="range" id="time_tolerance" min="0" max="1000" step="5" value="{{ p.time_tolerance }}" oninput="document.getElementById('val_time_tolerance').innerText=this.value">
                </div>
                <div class="form-group">
                    <label title="{{ tip.custom_shift }}">Shift OCR sub (ms)</label>
                    <input type="text" id="custom_shift" value="{{ p.custom_shift }}">
                </div>
                <div class="form-group">
                    <label title="{{ tip.max_line_length }}">Max line length (characters)</label>
                    <input type="text" id="max_line_length" value="{{ p.max_line_length }}">
                </div>
            </div>

            <button type="button" class="btn" onclick="submitConfig()">Start Processing</button>
            <div id="loading">Processing subtitles, please wait...</div>
        </form>
    </div>

    <script>
        // Open a native file/folder dialog (on the computer running syncsub) and fill the path
        function browse(id, kind) {
            const input = document.getElementById(id);
            fetch('/browse?kind=' + kind + '&current=' + encodeURIComponent(input.value))
                .then(response => response.json())
                .then(res => { if (res.path) input.value = res.path; });
        }

        function submitConfig() {
            document.getElementById('loading').style.display = 'block';
            const data = {
                origin_sub_path: document.getElementById('origin_sub_path').value,
                ocr_sub_path: document.getElementById('ocr_sub_path').value,
                output_filename: document.getElementById('output_filename').value,
                comment_timed_sub: document.getElementById('comment_timed_sub').checked,
                is_use_sign_timed: document.getElementById('is_use_sign_timed').checked,
                is_cmt_sign_timed: document.getElementById('is_cmt_sign_timed').checked,
                is_use_sign_ocr: document.getElementById('is_use_sign_ocr').checked,
                same_rate: parseFloat(document.getElementById('same_rate').value),
                time_tolerance: parseInt(document.getElementById('time_tolerance').value),
                custom_shift: parseFloat(document.getElementById('custom_shift').value) || 0,
                max_line_length: parseInt(document.getElementById('max_line_length').value) || 62
            };

            fetch('/process', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(data)
            })
            .then(response => response.json())
            .then(res => {
                if(res.success) {
                    window.location.href = '/editor';
                } else {
                    alert('Error: ' + res.message);
                    document.getElementById('loading').style.display = 'none';
                }
            })
            .catch(err => {
                alert('Request failed');
                document.getElementById('loading').style.display = 'none';
            });
        }
    </script>
</body>
</html>
""", p=dict(parameter, **batch["form"]), tip=default_config["Tooltip"])

@app.route('/browse')
def browse():
    # Native file/folder dialog on this computer (a web page can't get full paths)
    import tkinter, tkinter.filedialog
    kind = request.args.get("kind", "file")
    current = request.args.get("current", "")
    initial = current if os.path.isdir(current) else os.path.dirname(current)
    initial = initial if os.path.isdir(initial) else None
    root = tkinter.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    if kind == "folder":
        path = tkinter.filedialog.askdirectory(parent=root, initialdir=initial)
    elif kind == "save":
        path = tkinter.filedialog.asksaveasfilename(parent=root, initialdir=initial, defaultextension=".ass",
                                                    filetypes=[("ASS subtitle", "*.ass")])
    else:
        path = tkinter.filedialog.askopenfilename(parent=root, initialdir=initial,
                                                  filetypes=[("Subtitle", " ".join("*" + e for e in sub_extensions)), ("All files", "*.*")])
    root.destroy()
    return jsonify({"path": os.path.normpath(path) if path else ""})

@app.route('/process', methods=['POST'])
def process_route():
    try:
        data = request.json
        for k, v in data.items():
            if k in parameter:
                parameter[k] = v
        res, msg = convert_parameter(save_recent=True)
        batch["queue"] = []
        batch["form"] = {}
        origin, ocr, output = data["origin_sub_path"], data["ocr_sub_path"], data["output_filename"]
        if os.path.isdir(origin) and os.path.isdir(ocr):
            output_folder = output if os.path.isdir(output) else origin
            batch["queue"] = pair_episodes(origin, ocr, output_folder)
            if len(batch["queue"]) == 0:
                return jsonify({'success': False, 'message': 'No matching episodes between 2 folders.'})
            batch["index"] = 0
            batch["form"] = {"origin_sub_path": origin, "ocr_sub_path": ocr, "output_filename": output}
            load_batch_item()
        elif not res:
            return jsonify({'success': False, 'message': msg})
        prepare()
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)})

@app.route('/editor')
def editor():
    til = Path(parameter["origin_sub_path"])
    til = str(til.name)
    if len(batch["queue"]) > 0:
        til = "[%d/%d] %s" % (batch["index"]+1, len(batch["queue"]), til)
    template_string = """
<!DOCTYPE html>
<html>
<head>
    <title>syncsub - editor</title>
    <style>
        body {
            font-family: Arial, sans-serif;
            background-color: #f5f5f5;
            padding: 0em; 12em;
        }
        .group-container {
            background-color: white;
            border-radius: 5px;
            box-shadow: 0 2px 5px rgba(0,0,0,0.1);
        }
        .columns {
            display: flex;
            justify-content: space-between;
        }
        .column {
            width: 50%;
        }
        .event {
            display: flex;
            flex-direction: column;
            background-color: #f9f9f9;
            padding: 5px 20px;
            margin-bottom: 5px;
            border-radius: 4px;
            position: relative;
        }
        .event-time {
            font-size: 0.6em;
            color: #666;
            width: 50%;
        }
        .event-content {
            display: flex;
            align-items: center;
            justify-content: space-between;
            margin-top: 2px;
        }
        .event-text {
            flex-grow: 1;
            white-space: nowrap;
            line-height: 1.2em;
        }
        .ass-tag {
            display: inline;
            cursor: pointer;
            background-color: #e8e8e8;
            border-radius: 3px;
            padding: 0px 3px;
            font-size: 0.75em;
            color: #888;
            vertical-align: middle;
            user-select: none;
            border: 1px solid #ddd;
            transition: background-color 0.2s;
        }
        .ass-tag:hover {
            background-color: #d0d0d0;
        }
        .ass-tag.expanded {
            background-color: #fff3cd;
            color: #555;
            font-size: 1em;
            border-color: #ffc107;
        }
        .event-text-b {
            border: 1px solid #ddd;
            border-radius: 3px;
        }
        .event-function {
            margin-left: 10px;
            background-color: #f0f0f0;
            border: 1px solid #ddd;
            border-radius: 3px;
            padding: 0px 8px;
            cursor: pointer;
            height: 28px;
            align-self: right;
        }
        .event-copy:hover {
            background-color: #e0e0e0;
        }
        .column_graph {
            min-width: 8%;
            display: flex;
            flex-direction: column;
            overflow-x: auto;
            padding: 0px 10px;
        }
        .group-type {
            font-size: 0.8em;
            color: #666;
            text-align: center;
        }
        .graph_area {
            display: flex;
            flex-direction: row;
            align-items: center;
            justify-content: space-around;
            padding: 5px 0px;
            overflow-x: auto;
        }
        .graph-image {
            height: 100px;
        }
        .time-marker {
            position: absolute;
            height: 100%;
            background-color: rgba(0, 123, 255, 0.3);
            border: 1px solid rgba(0, 123, 255, 0.6);
            border-radius: 2px;
        }
        .empty-message {
            display: grid;
            justify-content: center;
            align-items: center;
            color: #999;
            font-size: 1.5rem;
            font-style: italic;
        }
        .submit-container {
            margin-top: 20px;
            margin-bottom: 60px;
            text-align: center;
            padding: 15px;
            background-color: white;
            border-radius: 5px;
            box-shadow: 0 2px 5px rgba(0,0,0,0.1);
        }
        .submit-button {
            background-color: #007bff;
            color: white;
            border: none;
            padding: 10px 20px;
            margin: 0px 10px;
            border-radius: 5px;
            font-size: 16px;
            cursor: pointer;
            transition: background-color 1s;
        }
        .scroll-button {
          position: fixed;
          bottom: 1%;
          right: 1%;
          outline: none;
          border: none;
          cursor: pointer;
          user-select: none;
          display: flex;
          flex-direction: row;
          align-items: center;
          gap: 8px;
          pointer-events: none;
          opacity: 0;
          transition: opacity 500ms ease;
          -webkit-tap-highlight-color: transparent;
        }
        .show-btn {
          opacity: 1 !important;
          pointer-events: all !important;
        }
    </style>
</head>
<body>
    <div>
        <h3>{{title}} <span id="draft-status" style="font-size: 0.6em; color: #888;"></span></h3>
    </div>
    <div id="groups-container">
    </div>
    <div class="submit-container">
        <button id="submit-button" class="submit-button">Submit</button>
        <div id="submit-result"></div>
    </div>
    <div class="scroll-button ">
        <button id="hide-button" class="submit-button" onclick="hide_button()">Hide/Show normal group</button>
        <button id="tag-toggle" class="submit-button" onclick="toggleAllTags()">Show/Hide {tags}</button>
        <button id="gototop" class="submit-button" title="Go to top">Go to top</button>
    </div>

    <script>
        // Long {tags} are shown collapsed as {…}, click to expand
        const TAG_COLLAPSE_THRESHOLD = 5;
        let allTagsExpanded = false;

        function collapseAssTags(text) {
            return text.replace(/{([^{}]+)}/g, function(match, inner) {
                if (inner.length > TAG_COLLAPSE_THRESHOLD) {
                    const escaped = match.replace(/"/g, '&quot;').replace(/'/g, '&#39;');
                    return `<span class="ass-tag" data-full="${escaped}" data-collapsed="{…}" onclick="toggleTag(this)">{…}</span>`;
                }
                return match;
            });
        }

        function toggleTag(el) {
            if (el.classList.contains('expanded')) {
                el.classList.remove('expanded');
                el.textContent = el.getAttribute('data-collapsed');
            } else {
                el.classList.add('expanded');
                el.textContent = el.getAttribute('data-full');
            }
        }

        function toggleAllTags() {
            allTagsExpanded = !allTagsExpanded;
            document.querySelectorAll('.ass-tag').forEach(el => {
                if (allTagsExpanded) {
                    el.classList.add('expanded');
                    el.textContent = el.getAttribute('data-full');
                } else {
                    el.classList.remove('expanded');
                    el.textContent = el.getAttribute('data-collapsed');
                }
            });
        }

        // Collapsed tags back to raw text before submit
        function restoreAssTags(html) {
            const temp = document.createElement('div');
            temp.innerHTML = html;
            temp.querySelectorAll('.ass-tag').forEach(el => {
                el.replaceWith(el.getAttribute('data-full'));
            });
            return temp.innerHTML;
        }

        // Timing graph of split_sub groups
        function createGraphImagesHtml(graphImages) {
            if (!graphImages || graphImages.length === 0) {
                return '<div class="empty-message"></div>';
            }

            let html = '';
            graphImages.forEach(graphData => {
                let max_level = graphData.max_level;
                let width = (max_level + 1) * 20;
                let rectHTML = `<div style="position: relative; width: ${width}px; height: 100px; margin-right: 15px;">`;
                graphData.rects.forEach(rect => {
                   rectHTML += `<div style="position: absolute; left: ${rect.level * 20}px; top: ${rect.top}%; height: ${rect.height}%; width: 18px; background-color: ${rect.color}; opacity: 0.8; border-radius: 2px;"></div>`;
                });
                rectHTML += '</div>';
                html += rectHTML;
            });

            return html;
        }

        // 1 group: timed lines | type + graph | editable ocr lines
        function createGroupHtml(group) {
            const groupA = group.group_a;
            const groupB = group.group_b;
            const colorA = group.color_a;
            const colorB = group.color_b;
            const graphImages = group.graphic || [];

            let displayStyle = group.type === "" ? 'style="display: none;"' : '';
            let html = `
                <div class="group-container" data-group-id="${group.web_id}" data-group-type="${group.type}" ${displayStyle}>
                    <div class="columns">
                        <div class="column">
                            ${groupA.length > 0 ? groupA.map((event, index) => {
                            return `<div class="event" data-group="a" data-index="${index}"}" style="border-right: 5px solid ${colorA[index]};">
                                    <div class="event-time">${event.start} > ${event.end}</div>
                                    <div class="event-content">
                                        <div class="event-text" " >${collapseAssTags(event.text)}</div>
                                        <button class='event-copy' onclick='copy_button(this)' >Copy</button>
                                    </div>
                                </div>
                            `}).join('') : ''}
                        </div>
                        <div class="column_graph">
                            <span class="group-type">${group.type}</span>
                            <div class="graph_area">
                                ${createGraphImagesHtml(graphImages)}
                            </div>
                        </div>

                        <div class="column">
                            ${groupB.length > 0 ? groupB.map((event, index) => {
                            let functionbutton = "";
                            if ( group.type === 'split_sub' ) {
                                functionbutton = "<button class='event-copy' onclick='combine_button(this)'>Combine</button>"
                            } else {
                                functionbutton = "<button class='event-copy' onclick='copy_button(this)' >Copy</button>"
                            }
                            let textColorB = index >= groupA.length ? 'color: gray;' : '';
                            return `<div class="event" data-group="b" data-index="${index}" style="border-left: 5px solid ${colorB[index]}">
                                    <div class="event-time">${event.start} > ${event.end}</div>
                                    <div class="event-content">
                                        <div class="event-text event-text-b" block-id="${group.web_id}" line-stt="${index}" style="${textColorB}" contenteditable>${collapseAssTags(event.text)}</div>
                                        ${functionbutton}
                                        <button class="event-function" onclick="clear_button(this)" >Clear</button>
                                    </div>
                                </div>
                            `}).join('') : ''}
                        </div>
                    </div>
                </div>
            `;

            return html;
        }

        let globalData = [];

        // Load groups
        function reload_data() {
            fetch('/data')
                .then(response => response.json())
                .then(data => {
                    globalData = data;
                    const container = document.getElementById('groups-container');
                    let allHtml = '';
                    data.forEach(group => {
                        allHtml += createGroupHtml(group);
                    });
                    container.innerHTML = allHtml;
                    restore_draft();
                })
                .catch(error => {
                    console.error('Error fetching data:', error);
                    document.getElementById('groups-container').innerHTML = '<div class="error">Error loading data</div>';
                });
        }

        reload_data();

        // Draft: autosave edited texts, restore them when editor is reopened
        let draftTimer = null;
        function save_draft() {
            const texts = {};
            document.querySelectorAll(".event-text-b").forEach(el => {
                texts[el.getAttribute("block-id") + "_" + el.getAttribute("line-stt")] = restoreAssTags(el.innerHTML);
            });
            fetch('/draft', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(texts)
            }).then(() => {
                document.getElementById("draft-status").innerText = "Draft saved " + new Date().toLocaleTimeString();
            });
        }
        function schedule_save() {
            clearTimeout(draftTimer);
            draftTimer = setTimeout(save_draft, 1000);
        }
        function restore_draft() {
            fetch('/draft')
                .then(response => response.json())
                .then(texts => {
                    if (Object.keys(texts).length === 0) return;
                    document.querySelectorAll(".event-text-b").forEach(el => {
                        const key = el.getAttribute("block-id") + "_" + el.getAttribute("line-stt");
                        if (key in texts) el.innerHTML = collapseAssTags(texts[key]);
                    });
                    document.getElementById("draft-status").innerText = "Draft restored";
                });
        }
        document.getElementById('groups-container').addEventListener('input', schedule_save);

        // Toggle 1-1 groups (hidden by default)
        function hide_button() {
            var normal_group = document.querySelectorAll("[data-group-type='']");
            normal_group.forEach(group => {
                  if (group.style.display === "none") {
                      group.style.display = "block";
                  } else {
                      group.style.display = "none";
                  }
            });
        }

        // Append next line to this one, shift the rest up
        function combine_button(button) {
            var textblock = button.closest('.column');
            var this_text = button.previousElementSibling;
            var thisid = parseInt(this_text.getAttribute("line-stt"));
            var textlines = textblock.querySelectorAll(".event-text");

            textlines.forEach(lines => {
                var temp_id = parseInt(lines.getAttribute("line-stt"));
                if (temp_id<=thisid) { }
                else if ( temp_id-thisid === 1 ) {
                    this_text.innerHTML = this_text.innerHTML + "<br>" + lines.innerHTML;
                    lines.innerHTML = "";
                }
                else if ( temp_id-thisid > 1 ) {
                    var previous_id = String(temp_id-1)
                    var templine = textblock.querySelector("div[line-stt='" + previous_id + "']")
                    templine.innerHTML = lines.innerHTML;
                    lines.innerHTML = "";
                }
            });
            schedule_save();
        };

        // Floating buttons appear after scrolling
        const scrollButton = document.querySelector(".scroll-button");
        window.addEventListener("scroll", () => {
          window.pageYOffset > 100
            ? scrollButton.classList.add("show-btn")
            : scrollButton.classList.remove("show-btn");
        });
        const gototop = document.querySelector("#gototop");
        gototop.addEventListener("click", () => {
          window.scrollTo({
            top: 0,
            behavior: "smooth"
          });
        });

        // Copy / Clear buttons
        function copy_button(element) {
          // As subtitle text: full {tags}, line break = \\N
          const temp = document.createElement('div');
          temp.innerHTML = restoreAssTags(element.previousElementSibling.innerHTML).replace(/<br\\s*\\/?>/gi, "\\\\N");
          navigator.clipboard.writeText(temp.textContent);
        }

        function clear_button(element) {
          var clearText = element.previousElementSibling.previousElementSibling;
          clearText.innerText = "";
          schedule_save();
        }

        // Submit edited texts; season mode then reloads with next episode
        document.getElementById('submit-button').addEventListener('click', function() {
            eventElements = document.getElementsByClassName("event-text")
            for (let i = 0; i < eventElements.length; i++) {
              const element = eventElements[i];
              if (element.hasAttribute("block-id")) {
                const blockId = parseInt(element.getAttribute("block-id"));
                const lineId = parseInt(element.getAttribute("line-stt"));
                const content = restoreAssTags(element.innerHTML);
                globalData[blockId].group_b[lineId]["text"] = content;
              }
            }
            const result = document.getElementById("submit-result");
            clearTimeout(draftTimer);
            fetch('/submit', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                },
                body: JSON.stringify(globalData)
            })
            .then(response => response.json())
            .then(res => {
                const skipped = (res.skipped || []).length > 0 ? "Skipped (failed to load): " + res.skipped.join("; ") : "";
                if (res.success && res.next) {
                    if (skipped) alert(skipped);
                    result.innerHTML = "Saved: " + res.output + "<br>Loading next episode...";
                    window.scrollTo(0, 0);
                    location.reload();
                } else if (res.success) {
                    document.getElementById("submit-button").style.display = "none";
                    result.innerText = "Saved: " + res.output + (skipped ? " | " + skipped : "");
                    result.innerHTML += "<br><a href='/'>Next file</a>";
                } else {
                    result.innerText = "Error: " + res.message;
                }
            })
            .catch(error => { result.innerText = "Request failed"; });
        });
    </script>
</body>
</html>
"""
    return render_template_string(template_string, title=til)

@app.route('/data')
def get_data():
    def make_dict(event):
        return {
            "start": pysubs2.time.ms_to_str(event.start),
            "start_ms": event.start,
            "end": pysubs2.time.ms_to_str(event.end),
            "end_ms": event.end,
            "text": html.escape(event.text, quote=False).replace("\\N","\n").replace("\n","<br>"),
            }

    obj_items = []
    for item in state.get("web_groups", []):
        obj_item = {
            'web_id': item['web_id'],
            'type': item['type'],
            'graphic': item['graphic'],
            'group_a': [make_dict(event) for event in item['group_a']],
            'group_b': [make_dict(event) for event in item['group_b']],
        }

        if item["type"] in ["from_ocr",""]:
            obj_item["color_a"] = ["#fff"]*len(item["group_a"])
        elif item["type"] in ["from_timed"]:
            obj_item['color_a'] = ["#ffff33"]*len(item["group_a"])
        else:
            obj_item['color_a'] = [gen_color(ie,lum=3/5) for ie in range(len(item['group_a']))]

        if item["type"] in ["from_timed",""]:
            obj_item["color_b"] = ["#fff"]*len(item["group_b"])
        else:
            obj_item['color_b'] = [gen_color(ie,lum=2/5,sat=2/3) for ie in range(len(item['group_b']))]
        obj_items.append(obj_item)
    return json.dumps(obj_items)

@app.route('/submit', methods=['POST'])
def submit():
    web_return = request.json
    if "web_groups" not in state or len(web_return) != len(state["web_groups"]):
        return jsonify({'success': False, 'message': 'Data does not match current file, please process again.'})
    try:
        draft = draft_path()
        output = finalize(web_return)
        if os.path.exists(draft):
            os.remove(draft)
        # Season mode: prepare next episode, skip episodes that fail to load
        skipped = []
        while batch["index"] + 1 < len(batch["queue"]):
            batch["index"] += 1
            load_batch_item()
            try:
                prepare()
                return jsonify({'success': True, 'output': output, 'next': True, 'skipped': skipped})
            except Exception as e:
                skipped += [os.path.basename(parameter["origin_sub_path"]) + ": " + str(e)]
        return jsonify({'success': True, 'output': output, 'skipped': skipped})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)})

@app.route('/draft', methods=['GET', 'POST'])
def draft():
    # Draft = edited texts of editor, only valid for the same input files and groups
    key = {"origin_sub_path": parameter["origin_sub_path"], "ocr_sub_path": parameter["ocr_sub_path"],
           "groups": len(state.get("web_groups", []))}
    if request.method == 'POST':
        with open(draft_path(), "w", encoding="utf-8") as f:
            json.dump(dict(key, texts=request.json), f, ensure_ascii=False)
        return jsonify({'success': True})
    try:
        with open(draft_path(), encoding="utf-8") as f:
            saved = json.load(f)
        if all(saved.get(k) == v for k, v in key.items()):
            return jsonify(saved["texts"])
    except Exception:
        pass
    return jsonify({})

if __name__ == '__main__':
    # Command line arguments override recent settings and skip the config page
    parser = argparse.ArgumentParser()
    for ta in parameter.keys():
        parser.add_argument('--'+ta , type=str, help=default_config["Tooltip"].get(ta))
    args = vars(parser.parse_args())
    start_page = "/"
    if any(v is not None for v in args.values()):
        for g in args:
            if args[g] is not None:
                parameter[g] = args[g]
        res, msg = convert_parameter()
        if not res:
            print(msg)
            sys.exit()
        prepare()
        start_page = "/editor"
    else:
        convert_parameter()

    # Port 5000, or a free one if something already listens there
    # (on Windows Flask can bind a busy port, then the other program gets the requests)
    port = 5000
    with socket.socket() as s:
        if s.connect_ex(("127.0.0.1", port)) == 0:
            with socket.socket() as s2:
                s2.bind(("127.0.0.1", 0))
                port = s2.getsockname()[1]

    def open_browser():
        time.sleep(0.5)
        webbrowser.open('http://127.0.0.1:%d%s' % (port, start_page))

    threading.Thread(target=open_browser, daemon=True).start()
    app.run(host='127.0.0.1', port=port)

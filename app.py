import os
import zipfile
import io
import json
import math
from collections import defaultdict
import uuid
import copy
import re
from flask import Flask, request, render_template, redirect, url_for, send_file, session, flash, after_this_request

# --- Deep unstringify/restringify helpers ------------------------------------

def is_json_string(s):
    return (
        isinstance(s, str)
        and ((s.strip().startswith("{") and s.strip().endswith("}"))
                or (s.strip().startswith("[") and s.strip().endswith("]")))
    )

def deep_unstringify(obj):
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if is_json_string(v):
                try:
                    v = deep_unstringify(json.loads(v))
                except Exception:
                    pass
            else:
                v = deep_unstringify(v)
            out[k] = v
        return out
    elif isinstance(obj, list):
        return [deep_unstringify(x) for x in obj]
    return obj

def deep_restringify(obj, template):
    # Recursively walk, stringify fields that were strings in template
    if isinstance(obj, dict) and isinstance(template, dict):
        out = {}
        for k, v in obj.items():
            # Only re-stringify fields that were stringified JSON in the template
            if k in template and is_json_string(template[k]):
                v = json.dumps(deep_restringify(v, json.loads(template[k])))
            else:
                v = deep_restringify(v, template.get(k, {}))
            out[k] = v
        return out
    elif isinstance(obj, list) and isinstance(template, list):
        # If template has items, use first as sub-template; else, leave as is
        tpl_item = template[0] if len(template) > 0 else {}
        return [deep_restringify(x, tpl_item) for x in obj]
    return obj

def are_structures_duplicate(a, b):
    """Returns True if structures have the same TypeID and nearly the same Position."""
    if a.get("TypeID") != b.get("TypeID"):
        return False
    pos_a = a.get("Position")
    pos_b = b.get("Position")
    if not (isinstance(pos_a, dict) and isinstance(pos_b, dict)):
        return False
    EPS = 0.02
    for axis in ("x", "y", "z"):
        try:
            if abs(float(pos_a.get(axis, 0)) - float(pos_b.get(axis, 0))) > EPS:
                return False
        except Exception:
            return False
    return True

def strip_is_duplicate(obj):
    if isinstance(obj, dict):
        if 'is_duplicate' in obj:
            del obj['is_duplicate']
        for v in obj.values():
            strip_is_duplicate(v)
    elif isinstance(obj, list):
        for v in obj:
            strip_is_duplicate(v)
    return obj

def extract_structures_from_any(base_json):
    # Unstringify up to 10 layers
    def unstringify(x):
        for _ in range(10):
            if isinstance(x, str) and (x.strip().startswith("{") or x.strip().startswith("[")):
                x = json.loads(x)
            else:
                break
        return x

    data = unstringify(base_json)
    structure_candidates = []
    meta = {
        "Name": data.get("Name"),
        "Author": data.get("Author"),
        "Description": data.get("Description"),
        "NumberOfElements": data.get("NumberOfElements"),
    }

    # First: save format (Data -> Constructions -> Structures)
    structures = None
    if isinstance(data, dict) and "Data" in data:
        d2 = unstringify(data["Data"])
        if isinstance(d2, dict) and "Constructions" in d2:
            cns = unstringify(d2["Constructions"])
            if isinstance(cns, dict) and "Structures" in cns:
                structures = cns["Structures"]
        elif isinstance(d2, dict) and "Structures" in d2:
            # Community base/export style
            structures = d2["Structures"]
    elif isinstance(data, dict) and "Structures" in data:
        # Structures directly in root (rare)
        structures = data["Structures"]

    # Now flatten
    if isinstance(structures, list):
        # Bucketed: list of lists (by TypeID) or flat (rare)
        for bucket in structures:
            if isinstance(bucket, list):
                for s in bucket:
                    if isinstance(s, dict):
                        structure_candidates.append(s)
            elif isinstance(bucket, dict):
                structure_candidates.append(bucket)
            # If fully empty (None), skip
    elif isinstance(structures, dict):
        structure_candidates.append(structures)

    # Fallback: if root structure is itself a list of dicts
    elif isinstance(data, list):
        for s in data:
            if isinstance(s, dict):
                structure_candidates.append(s)
    elif isinstance(data, dict) and "TypeID" in data:
        structure_candidates.append(data)

    return structure_candidates, meta

def normalize_imported_structure(structure, keep_linked=True):
    """Normalize imported structure while preserving relationships."""
    s = copy.deepcopy(structure)
    if not keep_linked:
        if "LinkedStructures" in s:
            original_length = len(s["LinkedStructures"])
            s["LinkedStructures"] = [None] * original_length
    if "Storages" not in s:
        s["Storages"] = []
    if "Scale" not in s:
        s["Scale"] = {"x": 1.0, "y": 1.0, "z": 1.0}
    return s

def distance(pos1, pos2):
    if not pos1 or not pos2:
        return float('inf')
    try:
        return math.sqrt(
            (float(pos1.get('x', 0)) - float(pos2.get('x', 0)))**2 +
            (float(pos1.get('y', 0)) - float(pos2.get('y', 0)))**2 +
            (float(pos1.get('z', 0)) - float(pos2.get('z', 0)))**2
        )
    except Exception:
        return float('inf')

def annotate_nearby(structures, threshold=0.25):
    # Add a list of nearby indices to each structure
    n = len(structures)
    for i in range(n):
        nearby = []
        pos_i = structures[i].get('Position')
        for j in range(n):
            if i == j: continue
            pos_j = structures[j].get('Position')
            if distance(pos_i, pos_j) < threshold:
                nearby.append(j)
        structures[i]['nearby_indices'] = nearby
    return structures

def structure_groups(structures, nearby_threshold=0.28):
    import math
    def distance(pos1, pos2):
        if not pos1 or not pos2:
            return float('inf')
        try:
            return math.sqrt(
                (float(pos1.get('x', 0)) - float(pos2.get('x', 0)))**2 +
                (float(pos1.get('y', 0)) - float(pos2.get('y', 0)))**2 +
                (float(pos1.get('z', 0)) - float(pos2.get('z', 0)))**2
            )
        except Exception:
            return float('inf')
    n = len(structures)
    adj = [[] for _ in range(n)]
    # Build adjacency graph for “nearby” and “linked”
    for i in range(n):
        # Nearby
        pi = structures[i].get('Position')
        for j in range(i+1, n):
            pj = structures[j].get('Position')
            if distance(pi, pj) < nearby_threshold:
                adj[i].append(j)
                adj[j].append(i)
        # # Linked
        # links = structures[i].get('LinkedStructures') or []
        # for link in links:
        #     if isinstance(link, int) and 0 <= link < n and link != i:
        #         adj[i].append(link)
        #         adj[link].append(i)
    # Find connected components (DFS)
    group = [None] * n
    group_counter = 0
    for i in range(n):
        if group[i] is not None:
            continue
        # Start a new group
        stack = [i]
        group[i] = group_counter
        while stack:
            node = stack.pop()
            for neighbor in adj[node]:
                if group[neighbor] is None:
                    group[neighbor] = group_counter
                    stack.append(neighbor)
        group_counter += 1
    # Annotate each structure with the group id
    for i, s in enumerate(structures):
        s['group_id'] = group[i]
        s['group_label'] = f"Structure Group {group[i] + 1}"
    return structures

# --- End helpers -------------------------------------------------------------

app = Flask(__name__)
# Load the secret key from an environment variable for security
app.secret_key = os.environ.get('SOTFSE_SECRET_KEY')
if not app.secret_key:
    raise ValueError("No SOTFSE_SECRET_KEY set for Flask application")

# Add these lines to increase limits
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024  # 50MB limit

UPLOAD_DIR = 'uploads'
os.makedirs(UPLOAD_DIR, exist_ok=True)

@app.context_processor
def inject_session_data():
    """Make session data available to all templates."""
    return dict(
        original_filename=session.get('original_filename'),
        game_stats=session.get('game_stats')
    )

@app.route('/', methods=['GET'])
def index():
    # Clear session data for a new upload
    session.pop('original_filename', None)
    session.pop('game_stats', None)
    return render_template('index.html')


@app.route('/upload', methods=['POST'])
def upload():
    file = request.files.get('file')
    if not file or not file.filename.endswith('.zip'):
        return "Please upload your SaveData.zip!", 400

    # Save zip to a temp file
    uid = str(uuid.uuid4())
    save_path = os.path.join(UPLOAD_DIR, f'{uid}.zip')
    file.save(save_path)

    game_stats = {}
    with zipfile.ZipFile(save_path) as zf:
        json_files = [f for f in zf.namelist() if f.endswith('.json')]
        
        # --- NEW: Extract stats and parent folder ---
        display_name = file.filename
        if json_files:
            # Get the directory part of the first file in the list
            parent_folder = os.path.dirname(json_files[0])
            if parent_folder:
                display_name = f"{parent_folder}/{file.filename}"

        # Find and parse GameStateSaveData.json for stats
        for f in json_files:
            if 'gamestatesavedata.json' in f.lower():
                try:
                    raw_game_state = zf.read(f).decode('utf-8')
                    game_state_data = json.loads(raw_game_state)
                    
                    # --- FIX: Unstringify the data before accessing it ---
                    unstringified_data = deep_unstringify(game_state_data)
                    state = unstringified_data.get("Data", {}).get("GameState", {})
                    
                    game_stats['Days'] = state.get('GameDays')
                    game_stats['Hours'] = state.get('GameHours')
                    game_stats['Type'] = state.get('GameType')
                    game_stats['Crash Site'] = state.get('CrashSite', 'N/A').title()

                except Exception as e:
                    print(f"Could not parse GameStateSaveData.json: {e}")
                break

    session['zip_filename'] = save_path
    session['json_files'] = json_files
    session['uid'] = uid
    session['original_filename'] = display_name
    session['game_stats'] = game_stats
    
    return render_template('options.html', files=json_files)

@app.route('/edit/<path:fname>', methods=['GET', 'POST'])
def edit_json(fname):
    zip_filename = session.get('zip_filename')
    if not zip_filename or not os.path.isfile(zip_filename):
        flash("Could not find uploaded ZIP—please start again!")
        return redirect(url_for('index'))

    # This GET part for displaying the editor remains the same.
    if request.method == 'GET':
        with zipfile.ZipFile(zip_filename) as zf:
            raw_json = zf.read(fname).decode('utf-8')
        try:
            json_data = json.loads(raw_json)
        except Exception as e:
            flash(f"Could not parse JSON: {e}")
            return redirect(url_for('index'))

        edit_session_id = str(uuid.uuid4())
        edit_dir = os.path.join(UPLOAD_DIR, f"edit_{edit_session_id}")
        os.makedirs(edit_dir, exist_ok=True)
        
        template_path = os.path.join(edit_dir, "template.json")
        with open(template_path, "w", encoding="utf-8") as f:
            f.write(raw_json)

        editable_data = deep_unstringify(json_data)
        
        # This part remains for displaying the content in the textarea.
        raw_json_for_textarea = json.dumps(editable_data, indent=2)

        session['edit_session_id'] = edit_session_id
        session['edit_fname'] = fname
        
        return render_template(
            'editor.html',
            fname=fname,
            raw_json=raw_json_for_textarea,
            edit_session_id=edit_session_id
        )

    # --- THIS IS THE MODIFIED POST LOGIC ---
    edit_session_id = session.get('edit_session_id')
    if not edit_session_id:
        flash("Edit session expired. Please start over.")
        return redirect(url_for('filelist'))

    edit_dir = os.path.join(UPLOAD_DIR, f"edit_{edit_session_id}")
    template_path = os.path.join(edit_dir, "template.json")

    if not os.path.exists(template_path):
        flash("Edit session files not found. Please start over.")
        return redirect(url_for('filelist'))

    # Get the edited data from either the file upload or the textarea
    if 'upload_mode' in request.form:
        uploaded_file = request.files.get('edited_file')
        if not uploaded_file:
            flash("No file uploaded!")
            return redirect(url_for('edit_json', fname=fname))
        try:
            edited = uploaded_file.read().decode('utf-8')
        except Exception as e:
            flash(f"Could not read uploaded file: {e}")
            return redirect(url_for('edit_json', fname=fname))
    else:
        edited = request.form.get('jtext')

    # Perform the CPU-intensive processing
    try:
        editable_data = json.loads(edited)
        with open(template_path, "r", encoding="utf-8") as f:
            raw_template = f.read()
        json_template = json.loads(raw_template)
        final_data = deep_restringify(editable_data, json_template)
        final_json_str = json.dumps(final_data, separators=(',', ':'))
    except Exception as e:
        return f"<b>Invalid JSON:</b> {e}", 500

    # --- THE NEW WORKFLOW: SAVE TO DISK, THEN REDIRECT ---
    
    # 1. Get the original zip path from the session.
    original_zip_path = session.get('zip_filename')
    
    # 2. Create a unique filename for the new downloadable zip.
    download_id = str(uuid.uuid4())
    new_zip_path = os.path.join(UPLOAD_DIR, f"download_{download_id}.zip")

    # 3. Create the new zip file on the server's disk.
    with zipfile.ZipFile(original_zip_path, 'r') as old_zip, \
         zipfile.ZipFile(new_zip_path, 'w') as new_zip:
        for item in old_zip.infolist():
            if item.filename == fname:
                new_zip.writestr(fname, final_json_str)
            else:
                new_zip.writestr(item, old_zip.read(item.filename))
    
    # 4. Save the path to this new file in the session so the next route can find it.
    session['downloadable_zip_path'] = new_zip_path
    
    # 5. Immediately redirect the user. This ends the POST request instantly and frees the worker.
    return redirect(url_for('download_zip'))

@app.route('/download-zip')
def download_zip():
    downloadable_zip_path = session.get('downloadable_zip_path')

    if not downloadable_zip_path or not os.path.exists(downloadable_zip_path):
        flash("No download file found or it has expired. Please try again.")
        return redirect(url_for('options'))

    # This decorator tells Flask to delete the file AFTER the user has
    # received it, cleaning up our temp files.
    @after_this_request
    def remove_file(response):
        try:
            os.remove(downloadable_zip_path)
            session.pop('downloadable_zip_path', None)
        except Exception as error:
            app.logger.error("Error removing or cleaning up file: %s", error)
        return response

    # Use send_file to send the file that's already on disk.
    # This is extremely efficient.
    return send_file(
        downloadable_zip_path,
        as_attachment=True,
        download_name="SaveData.zip"
    )

# Add this new route for downloading JSON files
@app.route('/download_json/<path:fname>')
def download_json(fname):
    zip_filename = session.get('zip_filename')
    if not zip_filename or not os.path.isfile(zip_filename):
        flash("Could not find uploaded ZIP!")
        return redirect(url_for('index'))
    
    with zipfile.ZipFile(zip_filename) as zf:
        raw_json = zf.read(fname).decode('utf-8')
    
    json_data = json.loads(raw_json)
    editable_data = deep_unstringify(json_data)
    editable_data = strip_is_duplicate(editable_data)
    formatted_json = json.dumps(editable_data, indent=2)
    
    return send_file(
        io.BytesIO(formatted_json.encode('utf-8')),
        mimetype='application/json',
        as_attachment=True,
        download_name=f"edited_{fname}"
    )

@app.route('/filelist', methods=['GET'])
def filelist():
    json_files = session.get('json_files', [])
    if not json_files:
        flash("No file list found in session, please upload your ZIP again.")
        return redirect(url_for('index'))
    return render_template('file_select.html', files=json_files)

@app.route('/options')
def options():
    # Only show this if there’s an uploaded file still active
    zip_filename = session.get('zip_filename')
    if not zip_filename or not os.path.isfile(zip_filename):
        flash("No uploaded save file found.")
        return redirect(url_for('index'))
    return render_template('options.html')

@app.route('/import_base_choose', methods=['GET', 'POST'])
def import_base_choose():
    if request.method == 'GET':
        return render_template('import_base_choose.html')

    basefile = request.files.get('basefile')
    if not basefile or not basefile.filename:
        flash("No base file was selected. Please choose a file.", "warning")
        return redirect(url_for('import_base_choose'))

    try:
        # Read the file content into a string to be safe
        basefile_content = basefile.read().decode('utf-8')
        if not basefile_content:
            flash("The uploaded file is empty.", "error")
            return redirect(url_for('import_base_choose'))
        
        base_json = json.loads(basefile_content)
        
        # Use helper to extract structures list and meta
        structure_candidates, base_meta = extract_structures_from_any(base_json)

        # Check for duplicates: build set of existing structure positions/typeIDs in user save
        zip_filename = session.get('zip_filename')
        constructions_fname = None
        for f in session.get('json_files', []):
            if 'construction' in f.lower():
                constructions_fname = f
                break
        
        existing_struct_positions = set()
        if constructions_fname and os.path.exists(zip_filename):
            with zipfile.ZipFile(zip_filename) as zf:
                try:
                    data = json.loads(zf.read(constructions_fname).decode('utf-8'))
                    data = deep_unstringify(data)
                    structs = data['Data']['Constructions']['Structures']
                    for tid, bucket in enumerate(structs):
                        if not isinstance(bucket, list):
                            continue
                        for s in bucket:
                            if not isinstance(s, dict):
                                continue
                            pos = s.get('Position')
                            struct_key = (
                                s.get('TypeID'),
                                round(float(pos.get('x', 0)), 2) if pos else None,
                                round(float(pos.get('y', 0)), 2) if pos else None,
                                round(float(pos.get('z', 0)), 2) if pos else None
                            )
                            existing_struct_positions.add(struct_key)
                except Exception as e:
                    print(f"[DEBUG] Could not read user save structures for dupe check: {e}")

        # Tag imported structures as duplicates or not
        for s in structure_candidates:
            pos = s.get('Position')
            struct_key = (
                s.get('TypeID'),
                round(float(pos.get('x', 0)), 2) if pos else None,
                round(float(pos.get('y', 0)), 2) if pos else None,
                round(float(pos.get('z', 0)), 2) if pos else None
            )
            s['is_duplicate'] = (struct_key in existing_struct_positions)

        # Store to temp and session
        base_temp_id = str(uuid.uuid4())
        structs_path = os.path.join(UPLOAD_DIR, f"{base_temp_id}_structs.json")
        meta_path = os.path.join(UPLOAD_DIR, f"{base_temp_id}_meta.json")
        with open(structs_path, "w", encoding="utf-8") as f:
            json.dump(structure_candidates, f)
        with open(meta_path, "w", encoding="utf-8") as f:
            json.dump(base_meta, f)
        
        session['base_temp_id'] = base_temp_id
        
        # Redirect to the correct page based on the button clicked
        if 'edit_and_import' in request.form:
            return redirect(url_for('view_and_edit_structures'))
        else:
            return redirect(url_for('import_base_select'))
            
    except json.JSONDecodeError as e:
        flash(f"Invalid JSON format. The file could not be parsed. Error: {e}", "error")
        return redirect(url_for('import_base_choose'))
    except Exception as e:
        flash(f"An unexpected error occurred: {e}", "error")
        return redirect(url_for('import_base_choose'))


@app.route('/import_base_select', methods=['GET', 'POST'])
def import_base_select():
    base_temp_id = session.get('base_temp_id')
    if not base_temp_id:
        return redirect(url_for('options'))
    structs_path = os.path.join(UPLOAD_DIR, f"{base_temp_id}_structs.json")
    meta_path = os.path.join(UPLOAD_DIR, f"{base_temp_id}_meta.json")
    # Check if files exist
    if not os.path.exists(structs_path) or not os.path.exists(meta_path):
        flash("Structure data files not found. Please upload your base file again.")
        return redirect(url_for('import_base_choose'))

    with open(meta_path, "r", encoding="utf-8") as f:
        base_meta = json.load(f)

    with open(structs_path, "r", encoding="utf-8") as f:
        structure_candidates = json.load(f)

    #structure_candidates = annotate_nearby(structure_candidates, threshold=0.28)  # tweak threshold here!
    structure_candidates = structure_groups(structure_candidates, nearby_threshold=5.00)

    # Group structures by their group_id:
    grouped_structures = defaultdict(list)
    for idx, s in enumerate(structure_candidates):
        group_label = s.get('group_label', f"Group {s.get('group_id', '?')}")
        grouped_structures[group_label].append((idx, s))
    
    return render_template(
        "import_base_select.html",
        grouped_structures=grouped_structures,
        structures=structure_candidates,  # in case your template needs it elsewhere
        base_meta=base_meta,
        group_count=len(grouped_structures),
    )

@app.route('/import_base_finish', methods=['POST'])
def import_base_finish():
    base_temp_id = session.get('base_temp_id')
    if not base_temp_id:
        return redirect(url_for('options'))
    
    structs_path = os.path.join(UPLOAD_DIR, f"{base_temp_id}_structs.json")
    if not os.path.exists(structs_path):
        flash("Structure data not found. Please start over.")
        return redirect(url_for('import_base_choose'))
    with open(structs_path, "r", encoding="utf-8") as f:
        structure_candidates = json.load(f)
    
    to_import_indices = request.form.getlist('import_ids')
    if not to_import_indices:
        return "No structures selected.", 400
    
    selected_structures = [structure_candidates[int(i)] for i in to_import_indices]

    # Handle duplicates
    count_dupe = sum(1 for s in selected_structures if s.get("is_duplicate"))
    if count_dupe:
        flash(f"{count_dupe} selected structure(s) look like duplicates. Test in-game for stability!", "warning")

    # Strip 'is_duplicate'
    selected_structures = [strip_is_duplicate(s) for s in selected_structures]

    # Find constructions file in save ZIP
    zip_filename = session.get('zip_filename')
    constructions_fname = None
    for f in session.get('json_files', []):
        if 'construction' in f.lower():
            constructions_fname = f
            break
    if not constructions_fname:
        return "No constructions file found in save!", 500
    diag_dir = os.path.join(UPLOAD_DIR, f"diag_{base_temp_id}")
    os.makedirs(diag_dir, exist_ok=True)
    # Read and parse the constructions file from ZIP
    with zipfile.ZipFile(zip_filename) as zf:
        raw_constructions = zf.read(constructions_fname).decode('utf-8')
        constructions_data = json.loads(raw_constructions)
        constructions_template = constructions_data.copy()
    # Fully unstringify for manipulation
    editable_cdata = deep_unstringify(constructions_data)
    cstructs = editable_cdata['Data']['Constructions']['Structures']

    # --- Group selected imported structures by TypeID ---
    import_groups = {}
    for s in selected_structures:
        tid = s.get("TypeID")
        if tid is not None:
            import_groups.setdefault(tid, []).append(s)

    # --- For each TypeID bucket, append batch and remap LinkedStructures ---
    for tid, structures in import_groups.items():
        # Ensure bucket exists in save
        while len(cstructs) <= tid:
            cstructs.append(None)
        if cstructs[tid] is None:
            cstructs[tid] = []
        elif not isinstance(cstructs[tid], list):
            cstructs[tid] = []    
        
        existing_len = len(cstructs[tid])
        import_count = len(structures)

        # Map original import indices to their new location in the save's structures array
        import_to_final_index = {i: existing_len + i for i in range(import_count)}
        
        remapped_structures = []
        for i, s in enumerate(structures):
            new_struct = normalize_imported_structure(s, keep_linked=True)
            # Remap links within imported batch. Out-of-batch links = None
            if "LinkedStructures" in new_struct and isinstance(new_struct["LinkedStructures"], list):
                new_links = []
                for link in new_struct["LinkedStructures"]:
                    if link is None:
                        new_links.append(None)
                    elif isinstance(link, int) and 0 <= link < import_count:
                        new_links.append(import_to_final_index[link])
                    elif isinstance(link, dict):
                        new_links.append(link)
                    else:
                        new_links.append(None)
                new_struct["LinkedStructures"] = new_links
            remapped_structures.append(new_struct)
        
        # Now add all newly-linked structures in batch
        cstructs[tid].extend(remapped_structures)

    # --- Write debug diagnostics ---
    with open(os.path.join(diag_dir, "original_structures.json"), "w", encoding="utf-8") as f:
        json.dump(editable_cdata['Data']['Constructions']['Structures'], f, indent=2)
    with open(os.path.join(diag_dir, "imported_structures.json"), "w", encoding="utf-8") as f:
        json.dump(selected_structures, f, indent=2)

    # --- Stringify, format numbers and output ---
    editable_cdata['Data']['Constructions']['Structures'] = cstructs
    new_cdata = deep_restringify(editable_cdata, constructions_template)
    new_cdata_str = json.dumps(new_cdata, separators=(',', ':'))
    
    new_cdata_str = re.sub(r'(\d+\.\d+)e(-?\d+)', r'\1E\2', new_cdata_str)
    with open(os.path.join(diag_dir, "final_constructions_raw.json"), "w", encoding="utf-8") as f:
        f.write(new_cdata_str)
    # --- Output new zip
    tmp_zip_io = io.BytesIO()
    with zipfile.ZipFile(zip_filename, 'r') as orig_zip, \
         zipfile.ZipFile(tmp_zip_io, 'w') as new_zip:
        for item in orig_zip.infolist():
            if item.filename == constructions_fname:
                new_zip.writestr(constructions_fname, new_cdata_str)
            else:
                new_zip.writestr(item, orig_zip.read(item.filename))
    tmp_zip_io.seek(0)
    return send_file(
        tmp_zip_io,
        mimetype='application/zip',
        as_attachment=True,
        download_name="SaveData.zip"
    )

@app.route('/debug_files')
def debug_files():
    """Show all debug/temp files for current session"""
    uid = session.get('uid')
    if not uid:
        flash("No active session found.")
        return redirect(url_for('index'))
    
    debug_files = []
    
    # Look for edit session files
    edit_session_id = session.get('edit_session_id')
    if edit_session_id:
        edit_dir = os.path.join(UPLOAD_DIR, f"edit_{edit_session_id}")
        if os.path.exists(edit_dir):
            for file in os.listdir(edit_dir):
                if file.endswith('.json'):
                    debug_files.append({
                        'name': f"edit_{edit_session_id}/{file}",
                        'path': os.path.join(edit_dir, file),
                        'size': os.path.getsize(os.path.join(edit_dir, file)),
                        'type': 'Edit Session'
                    })
    
    # Look for base import files
    base_temp_id = session.get('base_temp_id')
    if base_temp_id:
        # Structure files
        structs_path = os.path.join(UPLOAD_DIR, f"{base_temp_id}_structs.json")
        if os.path.exists(structs_path):
            debug_files.append({
                'name': f"{base_temp_id}_structs.json",
                'path': structs_path,
                'size': os.path.getsize(structs_path),
                'type': 'Import Structures'
            })
        
        # Diagnostic files
        diag_dir = os.path.join(UPLOAD_DIR, f"diag_{base_temp_id}")
        if os.path.exists(diag_dir):
            for file in os.listdir(diag_dir):
                if file.endswith('.json') or file.endswith('.txt'):
                    debug_files.append({
                        'name': f"diag_{base_temp_id}/{file}",
                        'path': os.path.join(diag_dir, file),
                        'size': os.path.getsize(os.path.join(diag_dir, file)),
                        'type': 'Diagnostics'
                    })
    
    return render_template('debug_files.html', debug_files=debug_files)

@app.route('/view_debug_file/<path:filename>')
def view_debug_file(filename):
    """View a debug file"""
    # Security: only allow files in our upload directory
    file_path = os.path.join(UPLOAD_DIR, filename)
    if not os.path.exists(file_path) or not file_path.startswith(UPLOAD_DIR):
        flash("File not found or access denied.")
        return redirect(url_for('debug_files'))
    
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read()
        
        # Try to pretty-print JSON
        try:
            json_data = json.loads(content)
            content = json.dumps(json_data, indent=2)
            file_type = "JSON"
        except:
            file_type = "Text"
        
        return render_template('view_debug_file.html', 
                               filename=filename, 
                               content=content, 
                               file_type=file_type)
    except Exception as e:
        flash(f"Error reading file: {e}")
        return redirect(url_for('debug_files'))

@app.route('/manage_structures')
def manage_structures():
    zip_filename = session.get('zip_filename')
    if not zip_filename or not os.path.isfile(zip_filename):
        flash("Could not find an uploaded ZIP file. Please start again.", "error")
        return redirect(url_for('index'))

    # Find the constructions file
    constructions_fname = None
    for f in session.get('json_files', []):
        if 'constructions' in f.lower() and f.endswith('.json'):
            constructions_fname = f
            break
    
    if not constructions_fname:
        flash("Could not find 'ConstructionsSaveData.json' in your save file.", "error")
        return redirect(url_for('options'))

    try:
        with zipfile.ZipFile(zip_filename) as zf:
            raw_json = zf.read(constructions_fname).decode('utf-8')
            json_data = json.loads(raw_json)

        # Use the helper to get a flat list of structures and group them
        structures, _ = extract_structures_from_any(json_data)
        structures_grouped = structure_groups(structures, nearby_threshold=5.00)

        # Store the flattened, grouped list for the deletion step
        manage_id = str(uuid.uuid4())
        structs_path = os.path.join(UPLOAD_DIR, f"manage_{manage_id}_structs.json")
        with open(structs_path, "w", encoding="utf-8") as f:
            json.dump(structures_grouped, f)
            
        session['manage_id'] = manage_id
        session['manage_fname'] = constructions_fname
        
        # Further group for the template
        grouped_for_template = defaultdict(list)
        for idx, s in enumerate(structures_grouped):
            group_label = s.get('group_label', f"Group {s.get('group_id', '?')}")
            grouped_for_template[group_label].append((idx, s))

        return render_template(
            'manage_structures.html', 
            grouped_structures=grouped_for_template,
            structure_count=len(structures_grouped)
        )

    except Exception as e:
        flash(f"Error reading your constructions file: {e}", "error")
        return redirect(url_for('options'))


@app.route('/delete_structures', methods=['POST'])
def delete_structures():
    zip_filename = session.get('zip_filename')
    manage_id = session.get('manage_id')
    constructions_fname = session.get('manage_fname')

    if not all([zip_filename, manage_id, constructions_fname]):
        flash("Your session has expired or is invalid. Please start over.", "error")
        return redirect(url_for('index'))

    structs_path = os.path.join(UPLOAD_DIR, f"manage_{manage_id}_structs.json")
    if not os.path.exists(structs_path):
        flash("Could not find the original structure data. Please start over.", "error")
        return redirect(url_for('manage_structures'))

    with open(structs_path, "r", encoding="utf-8") as f:
        original_structures = json.load(f) # This is the flat list

    # Get the indices to DELETE from the form
    indices_to_delete = {int(i) for i in request.form.getlist('delete_ids')}
    
    # Create a new list containing only the structures we want to KEEP
    structures_to_keep = [s for i, s in enumerate(original_structures) if i not in indices_to_delete]

    # Re-bucket the KEPT structures by TypeID
    buckets = defaultdict(list)
    for s in structures_to_keep:
        # We don't need the grouping keys anymore, remove them
        s.pop('group_id', None)
        s.pop('group_label', None)
        buckets[s['TypeID']].append(s)
    
    max_tid = max(buckets.keys()) if buckets else -1
    new_structure_list = [buckets.get(i) for i in range(max_tid + 1)]
    
    # Read the original constructions file to get the template
    with zipfile.ZipFile(zip_filename) as zf:
        raw_constructions = zf.read(constructions_fname).decode('utf-8')
        constructions_template = json.loads(raw_constructions)
    
    # Deep unstringify, replace structures, and deep restringify
    editable_cdata = deep_unstringify(constructions_template)
    editable_cdata['Data']['Constructions']['Structures'] = new_structure_list
    final_data = deep_restringify(editable_cdata, constructions_template)
    final_json_str = json.dumps(final_data, separators=(',', ':'))

    # --- Output new zip ---
    tmp_zip_io = io.BytesIO()
    with zipfile.ZipFile(zip_filename, 'r') as orig_zip, \
         zipfile.ZipFile(tmp_zip_io, 'w') as new_zip:
        for item in orig_zip.infolist():
            if item.filename == constructions_fname:
                new_zip.writestr(constructions_fname, final_json_str)
            else:
                new_zip.writestr(item, orig_zip.read(item.filename))
    tmp_zip_io.seek(0)
    
    flash(f"{len(indices_to_delete)} structures have been successfully deleted!", "success")
    return send_file(
        tmp_zip_io,
        mimetype='application/zip',
        as_attachment=True,
        download_name="SaveData_Modified.zip"
    )

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=False)
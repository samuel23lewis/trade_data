from flask import Flask, request, jsonify, render_template, send_from_directory, redirect, url_for, session, send_file
import os
import shutil
import hashlib
import pandas as pd
from datetime import datetime
from sqlalchemy import create_engine, text, inspect
import threading
import time
import uuid
import json
import mysql.connector
from mysql.connector import pooling
from werkzeug.utils import secure_filename
import csv

app = Flask(__name__)
app.secret_key = "your_secret_key"  # Required for session management

# --- SHARED DATABASE CONFIGURATION ---
DB_CONFIG = {
    "host": "localhost",
    "user": "root",
    "password": "123456",
    "database": "storage"
}

# --- CONNECTION MANAGEMENT ---
# Create a connection pool for lightweight queries
connection_pool = pooling.MySQLConnectionPool(pool_name="mypool", pool_size=5, **DB_CONFIG)

def get_db_connection():
    return connection_pool.get_connection()

def get_sqlalchemy_engine():
    return create_engine(f'mysql+pymysql://{DB_CONFIG["user"]}:{DB_CONFIG["password"]}@{DB_CONFIG["host"]}/{DB_CONFIG["database"]}')

# --- CONSTANTS FROM BOTH APPLICATIONS ---
# Table names
mysql_table = 'my_table'
log_table = 'processed_files_log'
invalid_log_table = 'invalid_files_log'
batch_size = 1000

# Excel file processing constants
COLUMNS_TO_DROP = [
    'Invoice No', 'Item Number', 'A_Group', 'City', 'State', 'Bill of Entry No', 'Importer_Address', 'PIN Code',
    'Supplier Address', 'Port code', 'Foreign Port', 'CHA Number', 'BE_Type', 'CUSH'
]

REQUIRED_COLUMNS = [
    'Bill of Entry Date', 'Importer ID', 'Importer_Name', 'Importer_City_State', 'Contact No', 'Email',
    'Supplier_Name', 'Origin Country', 'HS Code', 'Chapter', 'Product Discription', 'Quantity', 'Unit Quantity',
    'Unit value as per Invoice', 'INVOICE_CURRENCY', 'Total Value In FC', 'Unit Rate in USD', 'Exchange Rate',
    'Total Value USD (Exchange', 'Unit_Price in INR', 'Assess_Value_In_INR', 'Duty', 'CHA_Name', 'INDIAN Port'
]

# Filter export constants
COLUMN_MAPPING = {
    "1": "Importer_Name",
    "2": "Importer_City_State",
    "3": "Contact No",  
    "4": "Email",
    "5": "Supplier_Name",
    "6": "Origin Country",
    "7": "Product Discription",
    "8": "CHA_Name",
    "9": "INDIAN Port",
    "10": "HS Code",
    "11": "Chapter"
}

# --- TRACKING JOBS FROM BOTH APPLICATIONS ---
upload_jobs = {}  # For file uploading
export_jobs = {}  # For export jobs

# --- HELPER FUNCTIONS ---
# File processing helpers from first app
def calculate_md5(file_path):
    hash_md5 = hashlib.md5()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(4096), b""):
            hash_md5.update(chunk)
    return hash_md5.hexdigest()

def is_duplicate(file_hash, conn):
    result = conn.execute(
        text(f"SELECT COUNT(*) FROM {log_table} WHERE md5_hash = :hash"),
        {"hash": file_hash}
    ).scalar()
    return result > 0

def log_file_metadata(file_path, file_hash, engine):
    file_name = os.path.basename(file_path)
    file_size = os.path.getsize(file_path)
    last_modified = datetime.fromtimestamp(os.path.getmtime(file_path))

    try:
        with engine.begin() as connection:
            # First, check if this file (by name) exists in invalid_files_log
            result = connection.execute(
                text(f"SELECT id FROM {invalid_log_table} WHERE file_name = :file_name"),
                {"file_name": file_name}
            ).fetchone()
            
            # If found in invalid log, delete it
            if result:
                connection.execute(
                    text(f"DELETE FROM {invalid_log_table} WHERE id = :id"),
                    {"id": result[0]}
                )
            
            # Then log to processed files
            connection.execute(text(f"""
                INSERT INTO {log_table} (file_name, file_path, file_size, last_modified, md5_hash)
                VALUES (:file_name, :file_path, :file_size, :last_modified, :md5_hash)
            """), {
                "file_name": file_name,
                "file_path": file_path,
                "file_size": file_size,
                "last_modified": last_modified,
                "md5_hash": file_hash
            })
        return True
    except Exception as e:
        print(f"Could not log file {file_name}: {e}")
        return False

def log_invalid_file(file_path, file_hash, missing_cols, extra_cols, engine):
    file_name = os.path.basename(file_path)
    file_size = os.path.getsize(file_path)
    last_modified = datetime.fromtimestamp(os.path.getmtime(file_path))

    try:
        with engine.begin() as connection:
            connection.execute(text(f"""
                INSERT INTO {invalid_log_table} 
                (file_name, file_path, file_size, last_modified, md5_hash, missing_columns, extra_columns)
                VALUES 
                (:file_name, :file_path, :file_size, :last_modified, :md5_hash, :missing_columns, :extra_columns)
            """), {
                "file_name": file_name,
                "file_path": file_path,
                "file_size": file_size,
                "last_modified": last_modified,
                "md5_hash": file_hash,
                "missing_columns": ', '.join(missing_cols),
                "extra_columns": ', '.join(extra_cols)
            })
        return True
    except Exception as e:
        print(f"Could not log invalid file {file_name}: {e}")
        return False

def insert_new_chapters(df, conn):
    """
    Insert new chapters into the chapter table if they don't already exist.
    
    Args:
        df: The DataFrame containing Chapter data
        conn: SQLAlchemy connection object (may already have an active transaction)
    """
    # Make sure Chapter column exists in the DataFrame
    if 'Chapter' not in df.columns:
        print("No 'Chapter' column found in DataFrame")
        return
        
    # Get unique chapters, convert to strings, and remove any NaN values
    unique_chapters = df['Chapter'].dropna().astype(str).unique()
    
    # Debug: Print chapters being processed
    print(f"Processing {len(unique_chapters)} unique chapters: {unique_chapters[:5]}...")
    
    # Don't start a new transaction - just use the connection as is
    # This allows it to work with an existing transaction
    for chapter in unique_chapters:
        # Ensure chapter is a valid string
        if not chapter or not isinstance(chapter, str):
            continue
            
        # Trim whitespace
        chapter = chapter.strip()
        if not chapter:
            continue
            
        # Check if chapter exists
        exists = conn.execute(
            text("SELECT COUNT(*) FROM chapter WHERE Chapter = :chapter"),
            {"chapter": chapter}
        ).scalar()
        
        # If chapter doesn't exist, insert it
        if not exists:
            try:
                conn.execute(
                    text("INSERT INTO chapter (Chapter) VALUES (:chapter)"),
                    {"chapter": chapter}
                )
                print(f"Inserted new chapter: {chapter}")
            except Exception as e:
                print(f"Error inserting chapter '{chapter}': {e}")

def insert_new_hs_codes(df, engine):
    """
    Insert new HS codes into the hs_code table if they don't already exist.
    Modified to handle existing transactions properly.
    
    Args:
        df: The DataFrame containing HS Code and Chapter data
        engine: SQLAlchemy engine
    """
    # Make sure required columns exist
    if 'Chapter' not in df.columns or 'HS Code' not in df.columns:
        print("Required columns 'Chapter' and/or 'HS Code' not found in DataFrame")
        return
    
    # Get unique HS codes with their chapters
    hs_df = df[['Chapter', 'HS Code']].dropna().drop_duplicates()
    
    # Debug info
    print(f"Processing {len(hs_df)} unique HS codes")
    
    # Create a new connection for this specific operation
    # This avoids transaction conflicts with the main connection
    with engine.connect() as connection:
        # Start a transaction specifically for this connection
        with connection.begin():
            for _, row in hs_df.iterrows():
                try:
                    chapter = str(row['Chapter']).strip()
                    hs_code = str(row['HS Code']).strip()
                    
                    if not chapter or not hs_code:
                        continue
                    
                    exists = connection.execute(
                        text("""
                            SELECT COUNT(*) FROM hs_code
                            WHERE Chapter = :chapter AND `HS Code` = :hs_code
                        """),
                        {"chapter": chapter, "hs_code": hs_code}
                    ).scalar()
                    
                    if not exists:
                        connection.execute(
                            text("""
                                INSERT INTO hs_code (Chapter, `HS Code`)
                                VALUES (:chapter, :hs_code)
                            """),
                            {"chapter": chapter, "hs_code": hs_code}
                        )
                except Exception as e:
                    print(f"Error processing HS Code {row.get('HS Code', 'unknown')} for Chapter {row.get('Chapter', 'unknown')}: {e}")

# Filter export helpers from second app
def generate_filter_hash(filter_data):
    filter_str = json.dumps(filter_data, sort_keys=True)
    return hashlib.sha256(filter_str.encode()).hexdigest()

def check_existing_filter(filter_data):
    filter_hash = generate_filter_hash(filter_data)
    
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)
        
        cursor.execute("SELECT * FROM filter_history WHERE filter_hash = %s", (filter_hash,))
        result = cursor.fetchone()
        
        return result
    except mysql.connector.Error as err:
        print(f"Error checking existing filter: {err}")
        return None
    finally:
        cursor.close()
        conn.close()

def save_filter_history(filter_data, filename, row_count):
    filter_hash = generate_filter_hash(filter_data)
    
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        cursor.execute("SELECT id FROM filter_history WHERE filter_hash = %s", (filter_hash,))
        existing = cursor.fetchone()
        
        if existing:
            cursor.execute("""
                UPDATE filter_history 
                SET filename = %s, created_at = NOW(), filter_data = %s, rows_count = %s 
                WHERE filter_hash = %s
            """, (filename, json.dumps(filter_data), row_count, filter_hash))
        else:
            cursor.execute("""
                INSERT INTO filter_history (filter_hash, filter_data, filename, created_at, rows_count)
                VALUES (%s, %s, %s, NOW(), %s)
            """, (filter_hash, json.dumps(filter_data), filename, row_count))
        
        conn.commit()
        
        cursor.execute("SELECT id FROM filter_history WHERE filter_hash = %s", (filter_hash,))
        result = cursor.fetchone()
        return result[0] if result else None
        
    except mysql.connector.Error as err:
        print(f"Error saving filter history: {err}")
        return None
    finally:
        cursor.close()
        conn.close()

def build_query_from_filters(filter_data):
    base_query = "SELECT * FROM my_table WHERE 1=1"
    params = []
    
    # Handle date range filter
    if filter_data.get('from_date') and filter_data.get('to_date'):
        base_query += " AND `Bill of Entry Date` BETWEEN %s AND %s"
        params.extend([filter_data['from_date'], filter_data['to_date']])
    
    # Handle HS codes or chapters
    if filter_data.get('hs_codes') and len(filter_data['hs_codes']) > 0:
        placeholders = ', '.join(['%s'] * len(filter_data['hs_codes']))
        base_query += f" AND `HS Code` IN ({placeholders})"
        params.extend(filter_data['hs_codes'])
    elif filter_data.get('chapters') and len(filter_data['chapters']) > 0:
        placeholders = ', '.join(['%s'] * len(filter_data['chapters']))
        base_query += f" AND `Chapter` IN ({placeholders})"
        params.extend(filter_data['chapters'])
    
    # Handle column filters - modified to group by column
    if filter_data.get('columnFilters'):
        # Group conditions by column
        column_conditions = {}
        for column_filter in filter_data['columnFilters']:
            field = COLUMN_MAPPING.get(column_filter['field'], column_filter['field'])
            
            if field not in column_conditions:
                column_conditions[field] = []
            
            for condition in column_filter['conditions']:
                column_conditions[field].append({
                    'operator': condition['operator'],
                    'value': condition['value']
                })
        
        # Build conditions for each column
        for field, conditions in column_conditions.items():
            if len(conditions) == 1:
                # Single condition - handle as before
                condition = conditions[0]
                operator = condition['operator']
                value = condition['value']
                
                if operator == "Equals":
                    base_query += f" AND `{field}` = %s"
                    params.append(value)
                elif operator == "Does Not Equal":
                    base_query += f" AND `{field}` != %s"
                    params.append(value)
                elif operator == "Contains":
                    base_query += f" AND `{field}` LIKE %s"
                    params.append(f"%{value}%")
                elif operator == "Does Not Contain":
                    base_query += f" AND `{field}` NOT LIKE %s"
                    params.append(f"%{value}%")
                elif operator == "Begins With":
                    base_query += f" AND `{field}` LIKE %s"
                    params.append(f"{value}%")
                elif operator == "Ends With":
                    base_query += f" AND `{field}` LIKE %s"
                    params.append(f"%{value}")
            else:
                # Multiple conditions for same column - combine with OR
                or_conditions = []
                or_params = []
                
                for condition in conditions:
                    operator = condition['operator']
                    value = condition['value']
                    
                    if operator == "Equals":
                        or_conditions.append(f"`{field}` = %s")
                        or_params.append(value)
                    elif operator == "Does Not Equal":
                        or_conditions.append(f"`{field}` != %s")
                        or_params.append(value)
                    elif operator == "Contains":
                        or_conditions.append(f"`{field}` LIKE %s")
                        or_params.append(f"%{value}%")
                    elif operator == "Does Not Contain":
                        or_conditions.append(f"`{field}` NOT LIKE %s")
                        or_params.append(f"%{value}%")
                    elif operator == "Begins With":
                        or_conditions.append(f"`{field}` LIKE %s")
                        or_params.append(f"{value}%")
                    elif operator == "Ends With":
                        or_conditions.append(f"`{field}` LIKE %s")
                        or_params.append(f"%{value}")
                
                # Combine OR conditions and add to main query
                base_query += " AND (" + " OR ".join(or_conditions) + ")"
                params.extend(or_params)
    
    return base_query, params

def generate_filename(filter_data):
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    parts = ["export"]
    
    if filter_data.get('from_date') and filter_data.get('to_date'):
        from_date = filter_data['from_date'].replace('-', '')
        to_date = filter_data['to_date'].replace('-', '')
        parts.append(f"date_{from_date}_to_{to_date}")
    
    if filter_data.get('chapters') and len(filter_data['chapters']) > 0:
        chapter_str = "_".join(filter_data['chapters'][:3])
        if len(filter_data['chapters']) > 3:
            chapter_str += "_etc"
        parts.append(f"ch_{chapter_str}")
    
    parts.append(timestamp)
    return f"{'-'.join(parts)}.csv"

def get_export_history():
    export_files = []
    export_dir = "export"
    
    if not os.path.exists(export_dir):
        os.makedirs(export_dir)
    
    filter_history = {}
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)
        
        cursor.execute("SELECT * FROM filter_history")
        for row in cursor.fetchall():
            filter_history[row['filename']] = {
                'filter_id': row['id'],
                'filter_data': json.loads(row['filter_data']),
                'rows': row['rows_count'],
                'created_date': row['created_at'].strftime("%Y-%m-%d %H:%M:%S")
            }
    except mysql.connector.Error as err:
        print(f"Error getting filter history: {err}")
    finally:
        cursor.close()
        conn.close()
    
    for filename in os.listdir(export_dir):
        if filename.endswith('.csv'):
            file_path = os.path.join(export_dir, filename)
            file_stats = os.stat(file_path)
            
            size_bytes = file_stats.st_size
            if size_bytes < 1024:
                size_str = f"{size_bytes} B"
            elif size_bytes < 1024 * 1024:
                size_str = f"{size_bytes/1024:.1f} KB"
            else:
                size_str = f"{size_bytes/(1024*1024):.1f} MB"
            
            created_time = datetime.fromtimestamp(file_stats.st_ctime)
            
            filter_data = None
            filter_id = None
            rows = 0
            created_date = created_time.strftime("%Y-%m-%d %H:%M:%S")
            
            if filename in filter_history:
                filter_data = filter_history[filename]['filter_data']
                filter_id = filter_history[filename]['filter_id']
                rows = filter_history[filename]['rows']
                created_date = filter_history[filename]['created_date']
                
                if filter_data and 'columnFilters' in filter_data:
                    for column_filter in filter_data['columnFilters']:
                        if 'field' in column_filter:
                            column_id = column_filter['field']
                            column_filter['field_name'] = COLUMN_MAPPING.get(column_id, column_id)
            
            if rows == 0:
                try:
                    with open(file_path, 'r', encoding='utf-8') as f:
                        reader = csv.reader(f)
                        rows = sum(1 for _ in reader) - 1
                except Exception as e:
                    print(f"Error counting rows in {filename}: {e}")
            
            export_files.append({
                'name': filename,
                'size': size_str,
                'created_date': created_date,
                'rows': rows,
                'filter_data': filter_data,
                'filter_id': filter_id
            })
    
    export_files.sort(key=lambda x: x['created_date'], reverse=True)
    return export_files

# --- PROCESSING FUNCTIONS ---
def process_excel_files(job_id, file_paths):
    upload_jobs[job_id]['status'] = 'processing'
    upload_jobs[job_id]['processed'] = 0
    upload_jobs[job_id]['total'] = len(file_paths)
    upload_jobs[job_id]['valid'] = 0
    upload_jobs[job_id]['invalid'] = 0
    upload_jobs[job_id]['errors'] = []
    upload_jobs[job_id]['file_details'] = {}
    upload_jobs[job_id]['insertion_status'] = 'not_started'
    upload_jobs[job_id]['rows_inserted'] = 0
    upload_jobs[job_id]['total_rows'] = 0
    
    engine = get_sqlalchemy_engine()
    combined_df = pd.DataFrame()
    
    try:
        # First pass: Process all files and collect valid data
        with engine.connect() as conn:
            for idx, file_path in enumerate(file_paths):
                file_name = os.path.basename(file_path)
                upload_jobs[job_id]['file_details'][file_name] = {'status': 'processing'}
                
                try:
                    file_hash = calculate_md5(file_path)
                    
                    if is_duplicate(file_hash, conn):
                        upload_jobs[job_id]['file_details'][file_name] = {
                            'status': 'skipped', 
                            'message': 'Duplicate file'
                        }
                        continue
                        
                    df = pd.read_excel(file_path)
                    df.columns = df.columns.str.strip()
                    
                    extra_columns = [col for col in df.columns if col not in REQUIRED_COLUMNS and col not in COLUMNS_TO_DROP]
                    missing_columns = [col for col in REQUIRED_COLUMNS if col not in df.columns]
                    
                    if extra_columns or missing_columns:
                        # Use a separate transaction for logging invalid files
                        with engine.begin() as log_conn:
                            log_invalid_file(file_path, file_hash, missing_columns, extra_columns, engine)
                        
                        upload_jobs[job_id]['invalid'] += 1
                        upload_jobs[job_id]['file_details'][file_name] = {
                            'status': 'invalid',
                            'missing': missing_columns,
                            'extra': extra_columns
                        }
                        continue
                        
                    df.drop(columns=[col for col in COLUMNS_TO_DROP if col in df.columns], inplace=True, errors='ignore')
                    combined_df = pd.concat([combined_df, df[REQUIRED_COLUMNS]], ignore_index=True)
                    
                    # Use a separate transaction for logging valid files
                    with engine.begin() as log_conn:
                        log_file_metadata(file_path, file_hash, engine)
                    
                    upload_jobs[job_id]['valid'] += 1
                    upload_jobs[job_id]['file_details'][file_name] = {'status': 'processed'}
                    
                except Exception as e:
                    upload_jobs[job_id]['errors'].append(f"Error processing {file_name}: {str(e)}")
                    upload_jobs[job_id]['file_details'][file_name] = {
                        'status': 'error',
                        'message': str(e)
                    }
                
                upload_jobs[job_id]['processed'] += 1
        
        # Second pass: Insert chapters and HS codes
        if not combined_df.empty:
            try:
                # Process chapters
                with engine.connect() as conn:
                    # Debug print to verify DataFrame columns
                    print(f"DataFrame columns: {combined_df.columns.tolist()}")
                    print(f"Chapter column sample: {combined_df['Chapter'].head().tolist()}")
                    
                    # Insert chapters
                    with conn.begin():
                        insert_new_chapters(combined_df, conn)
                
                # Process HS codes (using separate function that creates its own connection)
                insert_new_hs_codes(combined_df, engine)
                
                # Third pass: Insert main data
                total_rows = len(combined_df)
                upload_jobs[job_id]['insertion_status'] = 'in_progress'
                upload_jobs[job_id]['total_rows'] = total_rows
                upload_jobs[job_id]['rows_inserted'] = 0
                
                # Check if table exists
                inspector = inspect(engine)
                table_exists = inspector.has_table(mysql_table)
                
                # Insert data in batches
                for start in range(0, total_rows, batch_size):
                    end = min(start + batch_size, total_rows)
                    batch_df = combined_df.iloc[start:end]
                    
                    if_exists = 'append' if table_exists or start > 0 else 'replace'
                    
                    batch_df.to_sql(
                        name=mysql_table, 
                        con=engine, 
                        if_exists=if_exists,
                        index=False,
                        method='multi'
                    )
                    
                    upload_jobs[job_id]['rows_inserted'] = end
                    table_exists = True  # After first batch, table definitely exists
                    
                upload_jobs[job_id]['data_rows'] = total_rows
                upload_jobs[job_id]['insertion_status'] = 'completed'
                
            except Exception as e:
                upload_jobs[job_id]['errors'].append(f"Error writing to database: {str(e)}")
                upload_jobs[job_id]['insertion_status'] = 'error'
    
    except Exception as e:
        upload_jobs[job_id]['errors'].append(f"General processing error: {str(e)}")
    finally:
        # Clean up resources
        engine.dispose()
        
        upload_jobs[job_id]['status'] = 'completed'
        upload_jobs[job_id]['end_time'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

        # Clean up temporary files
        for file_path in file_paths:
            if os.path.exists(file_path):
                try:
                    os.remove(file_path)
                except Exception as e:
                    upload_jobs[job_id]['errors'].append(f"Error removing file {os.path.basename(file_path)}: {str(e)}")

def process_export(export_id, filter_data):
    try:
        export_jobs[export_id]['status'] = 'processing'
        
        query, params = build_query_from_filters(filter_data)
        count_query = query.replace("SELECT *", "SELECT COUNT(*)")
        
        conn = get_db_connection()
        cursor = conn.cursor()
        
        cursor.execute(count_query, tuple(params))
        total_rows = cursor.fetchone()[0]
        export_jobs[export_id]['total_rows'] = total_rows
        
        if total_rows == 0:
            export_jobs[export_id]['status'] = 'completed'
            export_jobs[export_id]['filename'] = 'no_results.csv'
            
            os.makedirs("export", exist_ok=True)
            with open(os.path.join("export", 'no_results.csv'), 'w', newline='') as f:
                f.write("No results found")
                
            filter_id = save_filter_history(filter_data, 'no_results.csv', 0)
            export_jobs[export_id]['filter_id'] = filter_id
            
            cursor.close()
            conn.close()
            return
        
        filename = generate_filename(filter_data)
        export_jobs[export_id]['filename'] = filename
        
        os.makedirs("export", exist_ok=True)
        
        cursor = conn.cursor(dictionary=True)
        cursor.execute(query, tuple(params))
        
        batch_size = 1000
        processed_rows = 0
        first_batch = True
        
        with open(os.path.join("export", filename), 'w', newline='', encoding='utf-8') as csvfile:
            fieldnames = None
            writer = None
            
            while True:
                batch = cursor.fetchmany(batch_size)
                if not batch:
                    break
                
                if first_batch:
                    fieldnames = batch[0].keys()
                    writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
                    writer.writeheader()
                    first_batch = False
                
                writer.writerows(batch)
                processed_rows += len(batch)
                export_jobs[export_id]['processed_rows'] = processed_rows
                time.sleep(0.1)
        
        export_jobs[export_id]['status'] = 'completed'
        filter_id = save_filter_history(filter_data, filename, total_rows)
        export_jobs[export_id]['filter_id'] = filter_id
        
    except Exception as e:
        print(f"Export error: {e}")
        export_jobs[export_id]['status'] = 'error'
        export_jobs[export_id]['error'] = str(e)
    finally:
        if 'cursor' in locals() and cursor:
            cursor.close()
        if 'conn' in locals() and conn:
            conn.close()

# --- ROUTES ---
@app.route('/')
def index():
    return render_template('index.html')

# --- UPLOAD ROUTES ---
@app.route('/upload')
def upload_index():
    return render_template('upload/index.html')

@app.route('/upload/status/<job_id>')
def upload_status_page(job_id):
    if job_id not in upload_jobs:
        return redirect(url_for('upload_index'))
    return render_template('upload/status.html', job_id=job_id)

@app.route('/upload/invalid_files')
def invalid_files_log():
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)
        
        cursor.execute(f"SELECT * FROM {invalid_log_table} ORDER BY processed_at DESC")
        invalid_files = cursor.fetchall()
        
        # Convert datetime objects to strings for template rendering
        for file in invalid_files:
            file['last_modified'] = file['last_modified'].strftime("%Y-%m-%d %H:%M:%S")
            file['processed_at'] = file['processed_at'].strftime("%Y-%m-%d %H:%M:%S")
            
        return render_template('upload/invalid_files.html', invalid_files=invalid_files)
        
    except mysql.connector.Error as err:
        print(f"Error getting invalid files log: {err}")
        return render_template('upload/invalid_files.html', invalid_files=[])
    finally:
        cursor.close()
        conn.close()

@app.route('/upload/files', methods=['POST'])
def upload_files():
    if 'files[]' not in request.files:
        return jsonify({'error': 'No files provided'}), 400

    uploaded_files = request.files.getlist('files[]')
    job_id = str(uuid.uuid4())
    upload_dir = 'uploads'
    os.makedirs(upload_dir, exist_ok=True)

    file_paths = []
    upload_jobs[job_id] = {
        'status': 'starting',
        'saved_files': [],  # Track filenames for cleanup
        'start_time': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'total': len(uploaded_files),
        'processed': 0
    }

    for file in uploaded_files:
        if file and file.filename.endswith(('.xlsx', '.xls')):
            # Sanitize and ensure unique filename
            filename = secure_filename(file.filename)
            file_path = os.path.join(upload_dir, filename)
            
            # Handle collisions
            counter = 1
            while os.path.exists(file_path):
                name, ext = os.path.splitext(filename)
                filename = f"{name}_{counter}{ext}"
                file_path = os.path.join(upload_dir, filename)
                counter += 1

            file.save(file_path)
            file_paths.append(file_path)
            upload_jobs[job_id]['saved_files'].append(filename)  # Track for cleanup

    thread = threading.Thread(target=process_excel_files, args=(job_id, file_paths))
    thread.start()
    return jsonify({'job_id': job_id})

@app.route('/upload/job/<job_id>')
def upload_job_status(job_id):
    if job_id not in upload_jobs:
        return jsonify({'error': 'Job not found'}), 404
    return jsonify(upload_jobs[job_id])

@app.route('/upload/jobs')
def list_upload_jobs():
    return jsonify(list(upload_jobs.keys()))

@app.route('/upload/cleanup', methods=['POST'])
def cleanup_upload_jobs():
    current_time = datetime.now()
    jobs_to_remove = []
    
    for job_id, job_data in upload_jobs.items():
        if job_data['status'] == 'completed':
            end_time = datetime.strptime(job_data['end_time'], '%Y-%m-%d %H:%M:%S')
            if (current_time - end_time).seconds > 3600:
                jobs_to_remove.append(job_id)
                for file_name in os.listdir('uploads'):
                    if file_name.startswith(job_id):
                        try:
                            os.remove(os.path.join('uploads', file_name))
                        except Exception as e:
                            print(f"Error deleting file {file_name}: {e}")
    
    for job_id in jobs_to_remove:
        del upload_jobs[job_id]
        
    return jsonify({'removed': len(jobs_to_remove)})

# --- EXPORT ROUTES ---
@app.route('/export')
def export_index():
    applied_filter = session.pop('applied_filter', None)
    filter_filename = session.pop('filter_filename', None)
    
    return render_template('export/main.html', columns=COLUMN_MAPPING, 
                         applied_filter=json.dumps(applied_filter) if applied_filter else None,
                         filter_filename=filter_filename)

@app.route('/export/get_chapters')
def get_chapters():
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT Chapter FROM chapter")
        chapters = [row[0] for row in cursor.fetchall()]
    except mysql.connector.Error as err:
        print(f"Error: {err}")
        chapters = []
    finally:
        cursor.close()
        conn.close()
    
    return jsonify(chapters)

@app.route('/export/get_hs_codes', methods=['POST'])
def get_hs_codes():
    data = request.json
    selected_chapters = data.get("chapters", [])
    
    if not selected_chapters:  
        return jsonify([])
    
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        placeholders = ', '.join(['%s'] * len(selected_chapters))
        query = f"SELECT `HS Code` FROM hs_code WHERE Chapter IN ({placeholders})"
        cursor.execute(query, tuple(selected_chapters))
        
        hs_codes = [row[0] for row in cursor.fetchall()]
    except mysql.connector.Error as err:
        print(f"Error: {err}")
        hs_codes = []
    finally:
        cursor.close()
        conn.close()
    
    return jsonify(hs_codes)

@app.route('/export/start', methods=['POST'])
def start_export():
    filter_data = request.json
    export_id = str(uuid.uuid4())
    
    existing_filter = check_existing_filter(filter_data)
    
    if existing_filter:
        export_jobs[export_id] = {
            'status': 'exists',
            'total_rows': existing_filter['rows_count'],
            'processed_rows': existing_filter['rows_count'],
            'filename': existing_filter['filename'],
            'filter_data': filter_data,
            'error': None,
            'filter_id': existing_filter['id']
        }
    else:
        export_jobs[export_id] = {
            'status': 'starting',
            'total_rows': 0,
            'processed_rows': 0,
            'filename': '',
            'filter_data': filter_data,
            'error': None
        }
        
        thread = threading.Thread(target=process_export, args=(export_id, filter_data))
        thread.daemon = True
        thread.start()
    
    return jsonify({'export_id': export_id})

@app.route('/export/loading_status')
def loading_status():
    export_id = request.args.get('export_id')
    if not export_id or export_id not in export_jobs:
        return redirect(url_for('export_index'))
    return render_template('export/loading-status.html')

@app.route('/export/status/<export_id>')
def export_status(export_id):
    if export_id not in export_jobs:
        return jsonify({'error': 'Export job not found'})
    return jsonify(export_jobs[export_id])

@app.route('/export/download/<export_id>')
def download_csv(export_id):
    if export_id not in export_jobs or export_jobs[export_id]['status'] not in ['completed', 'exists']:
        return "Export not found or not completed", 404
    
    csv_path = os.path.join("export", export_jobs[export_id]['filename'])
    return send_file(csv_path, as_attachment=True, download_name=export_jobs[export_id]['filename'])

@app.route('/export/download_file/<filename>')
def download_export(filename):
    csv_path = os.path.join("export", filename)
    if not os.path.exists(csv_path):
        return "File not found", 404
    
    return send_file(csv_path, as_attachment=True, download_name=filename)

@app.route('/export/history')
def export_history():
    export_files = get_export_history()
    return render_template('export/history.html', export_files=export_files)

@app.route('/export/apply_filter/<int:filter_id>')
def apply_filter(filter_id):
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)
        
        cursor.execute("SELECT filter_data, filename FROM filter_history WHERE id = %s", (filter_id,))
        result = cursor.fetchone()
        
        if not result:
            return redirect(url_for('export_history'))
        
        session['applied_filter'] = json.loads(result['filter_data'])
        session['filter_filename'] = result['filename']
        
        return redirect(url_for('export_index'))
        
    except mysql.connector.Error as err:
        print(f"Error applying filter: {err}")
        return redirect(url_for('export_history'))
    finally:
        cursor.close()
        conn.close()

# --- INITIALIZATION ---
def initialize_database():
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS filter_history (
            id INT AUTO_INCREMENT PRIMARY KEY,
            filter_hash VARCHAR(64) NOT NULL UNIQUE,
            filter_data JSON NOT NULL,
            filename VARCHAR(255) NOT NULL,
            created_at DATETIME NOT NULL,
            rows_count INT NOT NULL DEFAULT 0
        )
        """)
        
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS processed_files_log (
            id INT AUTO_INCREMENT PRIMARY KEY,
            file_name VARCHAR(255) NOT NULL,
            file_path VARCHAR(255) NOT NULL,
            file_size INT NOT NULL,
            last_modified DATETIME NOT NULL,
            md5_hash VARCHAR(32) NOT NULL UNIQUE,
            processed_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
        """)
        
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS invalid_files_log (
            id INT AUTO_INCREMENT PRIMARY KEY,
            file_name VARCHAR(255) NOT NULL,
            file_path VARCHAR(255) NOT NULL,
            file_size INT NOT NULL,
            last_modified DATETIME NOT NULL,
            md5_hash VARCHAR(32) NOT NULL UNIQUE,
            missing_columns TEXT,
            extra_columns TEXT,
            processed_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
        """)
        
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS chapter (
            Chapter VARCHAR(50) NOT NULL UNIQUE
        )
        """)
        
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS hs_code (
            Chapter VARCHAR(50) NOT NULL,
            `HS Code` VARCHAR(50) NOT NULL,
            UNIQUE KEY chapter_hs_code (Chapter, `HS Code`)
        )
        """)
        
        conn.commit()
    except mysql.connector.Error as err:
        print(f"Database initialization error: {err}")
    finally:
        cursor.close()
        conn.close()

@app.before_request
def before_request():
    os.makedirs('uploads', exist_ok=True)
    os.makedirs('export', exist_ok=True)
    initialize_database()

if __name__ == '__main__':
    app.run(debug=True)

    
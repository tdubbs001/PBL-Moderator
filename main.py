import os
import psycopg2
from psycopg2.extras import Json
from flask import Flask, request, jsonify, render_template
from openai import OpenAI
import functions
from datetime import datetime
import json

app = Flask(__name__)

# Init client
client = OpenAI(api_key=os.getenv('OPENAI_API_KEY'))

# Create new assistant or load existing
assistant_id = functions.create_assistant(client)

# Initialize PostgreSQL connection
DATABASE_URL = os.getenv('DATABASE_URL')
conn = psycopg2.connect(DATABASE_URL)

def init_db():
    with conn.cursor() as cur:
        cur.execute('''
            CREATE TABLE IF NOT EXISTS conversations (
                id SERIAL PRIMARY KEY,
                thread_id TEXT,
                role TEXT,
                speaker TEXT,
                message TEXT,
                timestamp TIMESTAMP
            )
        ''')
    conn.commit()

# Call init_db() at the start of your application
init_db()

# Create the transcript folder with today's date
today_date_folder = os.path.join('transcripts', datetime.utcnow().strftime("%Y-%m-%d"))
if not os.path.exists(today_date_folder):
    os.makedirs(today_date_folder)

# Dictionary to store thread IDs for each role
role_threads = {}

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/start', methods=['GET'])
def start_conversation():
    role = request.args.get('role')
    if role in role_threads:
        return jsonify({"thread_id": role_threads[role]})
    thread = client.beta.threads.create()
    role_threads[role] = thread.id
    return jsonify({"thread_id": thread.id})

@app.route('/chat', methods=['POST'])
def chat():
    data = request.json
    user_role = data.get('role', '')
    thread_id = role_threads.get(user_role)
    if not thread_id:
        return jsonify({"error": "No active conversation for this role"}), 400
    user_input = data.get('message', '')

    # Create role-specific folder
    role_folder = os.path.join(today_date_folder, user_role)
    if not os.path.exists(role_folder):
        os.makedirs(role_folder)

    # Update log_filename to use the new folder structure
    log_filename = os.path.join(role_folder, f"thread_{thread_id}.json")

    def append_message_to_json_file(filename, thread_id, speaker, message):
        entry = {
            "speaker": speaker,
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "message": message
        }
        try:
            with open(filename, "r+") as f:
                data = json.load(f)
                if thread_id not in data:
                    data[thread_id] = []
                data[thread_id].append(entry)
                f.seek(0)
                json.dump(data, f, indent=4)
        except (FileNotFoundError, json.JSONDecodeError):
            with open(filename, "w") as f:
                json.dump({thread_id: [entry]}, f, indent=4)

    def save_message_to_db(thread_id, role, speaker, message):
        try:
            with conn.cursor() as cur:
                cur.execute('''
                    INSERT INTO conversations (thread_id, role, speaker, message, timestamp)
                    VALUES (%s, %s, %s, %s, %s)
                ''', (thread_id, role, speaker, message, datetime.utcnow()))
            conn.commit()
        except psycopg2.Error as e:
            print(f"Database error: {e}")
            conn.rollback()

    append_message_to_json_file(log_filename, thread_id, "user", user_input)
    save_message_to_db(thread_id, user_role, "user", user_input)

    # Add the user's role and message to the thread
    client.beta.threads.messages.create(
        thread_id=thread_id,
        role="user",
        content=f"[Role: {user_role}] {user_input}"
    )

    run = client.beta.threads.runs.create(
        thread_id=thread_id,
        assistant_id=assistant_id
    )

    while True:
        run_status = client.beta.threads.runs.retrieve(
            thread_id=thread_id,
            run_id=run.id
        )
        if run_status.status == 'completed':
            break

    messages = client.beta.threads.messages.list(thread_id=thread_id)
    assistant_response = messages.data[0].content[0].text.value

    append_message_to_json_file(log_filename, thread_id, "assistant", assistant_response)
    save_message_to_db(thread_id, user_role, "assistant", assistant_response)

    return jsonify({"response": assistant_response})

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)

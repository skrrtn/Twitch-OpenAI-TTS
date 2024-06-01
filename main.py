import socket
import threading
import re
import queue
import json
import requests
from datetime import datetime, timedelta
import os
import random
import time
import sounddevice as sd
import soundfile as sf
import pyttsx3

with open('config.json', 'r') as f:
    config = json.load(f)

required_keys = ['openai', 'twitch', 'system', 'limits']
for key in required_keys:
    if key not in config:
        raise ValueError(f"Missing required config section: {key}")

openai_api_key = config['openai']['api_key']

with open('random.txt', 'r') as f:
    random_questions = [line.strip() for line in f if line.strip()]

bad_words = []
if config['twitch'].get('bad_word_filter_enabled', True):
    if not os.path.exists('badwords.txt'):
        with open('badwords.txt', 'w') as f:
            f.write("")
    else:
        with open('badwords.txt', 'r') as f:
            bad_words = [line.strip().lower() for line in f if line.strip()]

class IRCClient:
    def __init__(self, config, question_queue):
        self.server = config['twitch']['server']
        self.port = config['twitch']['port']
        self.nickname = config['twitch']['nickname']
        self.token = config['twitch']['token']
        self.channel = config['twitch']['channel']
        self.sock = socket.socket()
        self.question_queue = question_queue
        self.user_last_question_time = {}
        self.user_question_interval = config['limits']['user_question_interval']
        self.char_limit = config['limits']['char_limit']
        self.random_questions_enabled = config['limits'].get('random_questions_enabled', True)
        self.random_question_idle_time = config['limits'].get('random_question_idle_time', 60)
        self.bad_word_filter_enabled = config['twitch'].get('bad_word_filter_enabled', True)
        self.timeout_seconds = config['twitch'].get('timeout_seconds', 600)

    def connect(self):
        try:
            self.sock.connect((self.server, self.port))
            self.sock.send(f"PASS {self.token}\n".encode('utf-8'))
            self.sock.send(f"NICK {self.nickname}\n".encode('utf-8'))
            self.sock.send(f"JOIN {self.channel}\n".encode('utf-8'))
            print("Connected to Twitch chat")
            self.listen()
        except Exception as e:
            print(f"Failed to connect to Twitch chat: {e}")

    def listen(self):
        def run():
            while True:
                try:
                    response = self.sock.recv(2048).decode('utf-8')
                    if response.startswith('PING'):
                        self.sock.send("PONG :tmi.twitch.tv\n".encode('utf-8'))
                    else:
                        self.handle_message(response)
                except Exception as e:
                    print(f"Error while listening to Twitch chat: {e}")

        thread = threading.Thread(target=run)
        thread.start()

    def handle_message(self, message):
        print(f"Received message: {message.strip()}")
        match_q = re.match(r'^:(\w+)!.* PRIVMSG #\w+ :!q (.+)', message)
        match_git = re.match(r'^:(\w+)!.* PRIVMSG #\w+ :!git', message)
        
        if match_q:
            username = match_q.group(1)
            question = match_q.group(2).strip()

            if self.bad_word_filter_enabled:
                for bad_word in bad_words:
                    if re.search(r'\b' + re.escape(bad_word) + r'\b', question, re.IGNORECASE):
                        self.sock.send(f"PRIVMSG {self.channel} :/timeout {username} {self.timeout_seconds}\n".encode('utf-8'))
                        time.sleep(0.5)  # Add a delay before notifying
                        self.sock.send(f"PRIVMSG {self.channel} :@{username} that language is not allowed.\n".encode('utf-8'))
                        print(f"User {username} used a bad word and was timed out.")
                        return

            if len(question) > self.char_limit:
                self.sock.send(f"PRIVMSG {self.channel} :@{username} your message exceeded the {self.char_limit} character limit!\n".encode('utf-8'))
                print(f"User {username}'s message exceeded the character limit.")
                return

            current_time = datetime.now()
            if username in self.user_last_question_time:
                last_ask_time = self.user_last_question_time[username]
                if current_time - last_ask_time < timedelta(seconds=self.user_question_interval):
                    print(f"User {username} must wait before asking another question.")
                    return

            self.user_last_question_time[username] = current_time
            self.question_queue.put((username, question))
            print(f"Queued question from {username}: {question}")
            
        elif match_git:
            username = match_git.group(1)
            self.sock.send(f"PRIVMSG {self.channel} :@{username} Here is the link to the GitHub repo: https://github.com/skrrtn/Twitch-OpenAI-TTS \n".encode('utf-8'))

class QuestionQueue:
    def __init__(self):
        self.queue = queue.Queue()
        self.last_question_time = datetime.now()

    def put(self, item):
        self.queue.put(item)
        self.last_question_time = datetime.now()

    def get(self):
        return self.queue.get()

    def empty(self):
        return self.queue.empty()

    def has_stale_queue(self, timeout_seconds=5):
        return datetime.now() - self.last_question_time > timedelta(seconds=timeout_seconds)

    def size(self):
        return self.queue.qsize()

def get_random_question():
    return random.choice(random_questions)

def get_openai_response(question, system_message, model, max_tokens=120):
    url = "https://api.openai.com/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {openai_api_key}",
        "Content-Type": "application/json"
    }
    payload = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": system_message
            },
            {
                "role": "user",
                "content": question
            }
        ],
        "max_tokens": max_tokens
    }
    response = requests.post(url, headers=headers, json=payload)
    if response.status_code == 200:
        return response.json()["choices"][0]["message"]["content"]
    else:
        print(f"Failed to get a response from OpenAI: {response.text}")
        return None

def generate_speech(response, tts_model, voice, filename):
    url = "https://api.openai.com/v1/audio/speech"
    headers = {
        "Authorization": f"Bearer {openai_api_key}",
        "Content-Type": "application/json"
    }
    payload = {
        "model": tts_model,
        "input": response,
        "voice": voice
    }
    response = requests.post(url, headers=headers, json=payload)
    if response.status_code == 200:
        with open(filename, 'wb') as f:
            f.write(response.content)
        return filename
    else:
        print(f"Failed to generate speech: {response.text}")
        return None
    
def save_question(question):
    question = ''.join(char for char in question if ord(char) < 128)
    
    lines = []
    current_line = ""
    for word in question.split():
        if len(current_line) + len(word) <= 45:
            current_line += word + " "
        else:
            lines.append(current_line.strip())
            current_line = word + " "
    if current_line:
        lines.append(current_line.strip())

    with open('question.txt', 'w', encoding='utf-8') as f:
        f.write("\n".join(lines))

def save_answer(answer):
    lines = []
    current_line = ""
    for word in answer.split():
        if len(current_line) + len(word) <= 45:
            current_line += word + " "
        else:
            lines.append(current_line.strip())
            current_line = word + " "
    if current_line:
        lines.append(current_line.strip())

    with open('response.txt', 'w', encoding='utf-8') as f: 
        f.write("\n".join(lines))

def clear_text_files():
    if os.path.exists("question.txt"):
        os.remove("question.txt")
    if os.path.exists("response.txt"):
        os.remove("response.txt")

def play_tts(filename, device_id):
    data, fs = sf.read(filename)
    sd.play(data, fs, device=device_id)
    sd.wait()
    os.remove(filename)

def play_question(question, device_id, username=None):
    engine = pyttsx3.init()
    engine.setProperty('rate', 130)
    engine.setProperty('volume', 0.85)
    voices = engine.getProperty('voices')
    engine.setProperty('voice', voices[0].id)
    if username:
        question = f"{username} asks, {question}"
    engine.save_to_file(question, 'temp.wav')
    engine.runAndWait()

    data, fs = sf.read('temp.wav')
    sd.play(data, fs, device=device_id)
    sd.wait()
    os.remove('temp.wav')

def update_queue_file(question_queue):
    while True:
        with open('queue.txt', 'w') as f:
            f.write(str(question_queue.size()))
        time.sleep(1)

def main():
    clear_text_files()
    question_queue = QuestionQueue()
    irc_client = IRCClient(config, question_queue)
    irc_client.connect()

    last_tts_time = datetime.now()
    random_question_interval = timedelta(seconds=config['limits'].get('random_question_idle_time', 60))
    tts_device_id_question = config['system'].get('question_tts_device_id')
    tts_device_id_response = config['system'].get('response_tts_device_id')

    with open('queue.txt', 'w') as f:
        f.write('0')

    update_thread = threading.Thread(target=update_queue_file, args=(question_queue,))
    update_thread.start()

    while True:
        if not question_queue.empty():
            username, question = question_queue.get()
            print(f"Processing question from {username}: {question}")
            clear_text_files()
            save_question(question)
            response = get_openai_response(
                question, 
                config['openai']['system_message'], 
                config['openai']['model']
            )
            if response:
                print(f"Generated response: {response}")
                save_answer(response)
                audio_filename = generate_speech(
                    response, 
                    config['openai']['tts_model'], 
                    config['openai']['voice'], 
                    'response.wav'
                )
                if audio_filename:
                    play_question(question, tts_device_id_question, username=username)
                    play_tts(audio_filename, tts_device_id_response)
                    clear_text_files()
                    last_tts_time = datetime.now()
            else:
                print("Failed to generate a response from OpenAI.")
        elif config['limits'].get('random_questions_enabled', True) and datetime.now() - last_tts_time >= random_question_interval:
            random_question = get_random_question()
            print(f"Asking random question: {random_question}")
            play_question(random_question, tts_device_id_question)
            clear_text_files()
            last_tts_time = datetime.now()
        time.sleep(1)

if __name__ == "__main__":
    main()
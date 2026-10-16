import sounddevice as sd
import soundfile as sf
import threading
from io import BytesIO

class AudioPlayer:
    def __init__(self, output_device=None, cable_output=None):
        self.output_device = output_device
        self.cable_output = cable_output
        self.playing = False
        self.lock = threading.Lock()

    def play(self, audio_data):
        self.stop()
        try:
            # 🔁 파일 경로인지 확인
            if isinstance(audio_data, str):
                with open(audio_data, "rb") as f:
                    audio_bytes = f.read()
            elif isinstance(audio_data, BytesIO):
                audio_bytes = audio_data.read()
            else:
                raise TypeError("지원되지 않는 오디오 타입")

            # 두 장치용으로 복사
            data1, sr1 = sf.read(BytesIO(audio_bytes), dtype='float32')
            data2, sr2 = sf.read(BytesIO(audio_bytes), dtype='float32')

            def play_and_wait(data, sr, device_index):
                try:
                    sd.play(data, samplerate=sr, device=device_index)
                    sd.wait()  # 재생 끝날 때까지 대기
                except Exception as e:
                    print(f"❌ 출력 장치 {device_index} 재생 실패: {e}")

            threads = []
            if self.output_device is not None:
                threads.append(threading.Thread(target=play_and_wait, args=(data1, sr1, self.output_device)))
            if self.cable_output is not None:
                threads.append(threading.Thread(target=play_and_wait, args=(data2, sr2, self.cable_output)))

            with self.lock:
                self.playing = True
                for t in threads:
                    t.start()
                for t in threads:
                    t.join()

        except Exception as e:
            print(f"❌ 재생 오류: {e}")
        finally:
            self.playing = False


    def stop(self):
        with self.lock:
            if self.playing:
                sd.stop()
                print("⏹️ 재생 중단됨")
                self.playing = False

    def is_playing(self):
        return self.playing

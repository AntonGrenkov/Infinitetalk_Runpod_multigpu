import runpod
# from runpod.serverless.utils import rp_upload
import os
import websocket
import base64
import json
import uuid
import logging
import urllib.request
import urllib.parse
import binascii  # Импорт для обработки ошибок Base64
import subprocess
import time
import librosa
import torch
from concurrent.futures import ThreadPoolExecutor, as_completed
import shutil
# Настройка логирования
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


server_address = os.getenv('SERVER_ADDRESS', '127.0.0.1')
client_id = str(uuid.uuid4())


def get_available_gpus():
    """Определяет количество доступных GPU в системе"""
    try:
        if torch.cuda.is_available():
            gpu_count = torch.cuda.device_count()
            logger.info(f"Обнаружено {gpu_count} GPU устройств")
            for i in range(gpu_count):
                gpu_name = torch.cuda.get_device_name(i)

                gpu_memory = \
                    torch.cuda.get_device_properties(i).total_memory / 1024**3

                logger.info(f"GPU {i}: {gpu_name} ({gpu_memory:.1f} GB)")
            return gpu_count
        else:
            logger.warning("CUDA недоступен, используем CPU")
            return 0
    except Exception as e:
        logger.error(f"Ошибка при определении GPU: {e}")
        return 0


def calculate_segments(total_frames, gpu_count, frame_window_size=81):
    """Вычисляет сегменты для каждого GPU"""
    if gpu_count <= 1:
        return [{"start_frame": 0, "end_frame": total_frames, "gpu_id": 0}]

    # Рассчитываем количество кадров на GPU с учетом перекрытия
    # 50% перекрытие для плавных переходов
    overlap_frames = frame_window_size // 2

    # Округление вверх
    frames_per_gpu = (total_frames + gpu_count - 1) // gpu_count

    segments = []
    for i in range(gpu_count):
        start_frame = i * frames_per_gpu
        end_frame = min(
            start_frame + frames_per_gpu + overlap_frames, total_frames
        )

        # Корректируем для первого и последнего сегмента
        if i == 0:
            start_frame = 0
        if i == gpu_count - 1:
            end_frame = total_frames

        segments.append({
            "start_frame": start_frame,
            "end_frame": end_frame,
            "gpu_id": i,
            "segment_id": i
        })

        logger.info(
            f"GPU {i}: кадры {start_frame}-{end_frame} \
                ({end_frame - start_frame} кадров)"
        )

    return segments


def create_segment_workflow(base_workflow, segment_info, temp_dir):
    """Создает отдельный воркфлоу для сегмента"""
    segment_workflow = json.loads(json.dumps(base_workflow))

    # Настраиваем параметры сегмента
    segment_workflow["270"]["inputs"]["value"] = \
        segment_info["end_frame"] - segment_info["start_frame"]

    # Добавляем информацию о сегменте в workflow
    segment_workflow["_segment_info"] = segment_info

    # Сохраняем workflow для сегмента
    segment_file = os.path.join(
        temp_dir,
        f"workflow_segment_{segment_info['segment_id']}.json"
    )
    with open(segment_file, 'w') as f:
        json.dump(segment_workflow, f, indent=2)

    return segment_file


def process_segment_on_gpu(segment_info, workflow_file, temp_dir, gpu_id):
    """Обрабатывает один сегмент на указанном GPU"""
    try:
        # Устанавливаем GPU для текущего потока
        os.environ['CUDA_VISIBLE_DEVICES'] = str(gpu_id)

        # Создаем отдельный ComfyUI процесс для этого GPU
        segment_output_dir = os.path.join(
            temp_dir,
            f"segment_{segment_info['segment_id']}"
        )
        os.makedirs(segment_output_dir, exist_ok=True)

        # Запускаем ComfyUI с портом для этого сегмента
        port = 8188 + segment_info['segment_id']

        logger.info(
            f"Запуск ComfyUI для сегмента \
                {segment_info['segment_id']} на GPU {gpu_id}, порт {port}"
        )

        # Команда для запуска ComfyUI
        comfy_cmd = [
            'python', '/ComfyUI/main.py',
            '--listen', '127.0.0.1',
            '--port', str(port),
            '--use-sage-attention'
        ]

        # Запускаем ComfyUI в фоне
        comfy_process = subprocess.Popen(
            comfy_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd='/ComfyUI'
        )

        # Ждем готовности ComfyUI
        max_wait = 60
        wait_count = 0
        while wait_count < max_wait:
            try:
                response = urllib.request.urlopen(
                    f'http://127.0.0.1:{port}/',
                    timeout=5
                )
                if response.getcode() == 200:
                    logger.info(
                        f"ComfyUI готов для сегмента \
                            {segment_info['segment_id']}"
                    )
                    break
            except Exception:
                pass
            time.sleep(2)
            wait_count += 2

        if wait_count >= max_wait:
            raise Exception(
                f"ComfyUI не запустился для сегмента \
                    {segment_info['segment_id']}"
            )

        # Обрабатываем сегмент
        result = process_single_segment(
            segment_info,
            workflow_file,
            port,
            segment_output_dir
        )

        # Завершаем ComfyUI процесс
        comfy_process.terminate()
        comfy_process.wait()

        return result

    except Exception as e:
        logger.error(
            f"Ошибка обработки сегмента {segment_info['segment_id']}: {e}"
        )
        return None


def process_single_segment(segment_info, workflow_file, port, output_dir):
    """Обрабатывает один сегмент через ComfyUI API"""
    try:
        # Загружаем workflow
        with open(workflow_file, 'r') as f:
            workflow = json.load(f)

        # Отправляем задачу в ComfyUI
        client_id = str(uuid.uuid4())
        url = f"http://127.0.0.1:{port}/prompt"
        data = json.dumps(
            {"prompt": workflow, "client_id": client_id}
        ).encode('utf-8')

        request = urllib.request.Request(
            url,
            data=data,
            headers={'Content-Type': 'application/json'}
        )
        response = urllib.request.urlopen(request, timeout=30)

        if response.getcode() != 200:
            raise Exception(f"Ошибка отправки задачи: {response.getcode()}")

        result = json.loads(response.read())
        prompt_id = result['prompt_id']

        # Ждем завершения обработки
        while True:
            history_url = f"http://127.0.0.1:{port}/history/{prompt_id}"
            response = urllib.request.urlopen(history_url, timeout=10)
            history = json.loads(response.read())

            if prompt_id in history:
                outputs = history[prompt_id]['outputs']
                if outputs:
                    # Ищем результат
                    for node_id, node_output in outputs.items():
                        if 'gifs' in node_output:
                            return node_output['gifs']
            time.sleep(1)

    except Exception as e:
        logger.error(f"Ошибка обработки сегмента: {e}")
        return None


def merge_video_segments(segment_files, output_path, temp_dir):
    """Склеивает сегменты видео в один файл"""
    try:
        if len(segment_files) == 1:
            # Если только один сегмент, просто копируем
            shutil.copy2(segment_files[0], output_path)
            return output_path

        # Создаем список файлов для ffmpeg
        concat_file = os.path.join(temp_dir, "concat_list.txt")
        with open(concat_file, 'w') as f:
            for segment_file in segment_files:
                f.write(f"file '{segment_file}'\n")

        # Склеиваем видео с помощью ffmpeg
        cmd = [
            'ffmpeg', '-f', 'concat', '-safe', '0', '-i', concat_file,
            '-c', 'copy', '-y', output_path
        ]

        logger.info(f"Склеивание сегментов: {' '.join(cmd)}")
        result = subprocess.run(cmd, capture_output=True, text=True)

        if result.returncode != 0:
            logger.error(f"Ошибка ffmpeg: {result.stderr}")
            raise Exception(f"Ошибка склеивания видео: {result.stderr}")

        logger.info(f"Видео успешно склеено: {output_path}")
        return output_path

    except Exception as e:
        logger.error(f"Ошибка склеивания сегментов: {e}")
        raise


def download_file_from_url(url, output_path):
    """Функция для загрузки файла из URL"""
    try:
        # Загрузка файла с помощью wget
        result = subprocess.run([
            'wget', '-O', output_path, '--no-verbose', '--timeout=30', url
        ], capture_output=True, text=True, timeout=60)

        if result.returncode == 0:
            logger.info(
                f"✅ Файл успешно загружен из URL: {url} -> {output_path}"
            )
            return output_path
        else:
            logger.error(f"❌ Ошибка загрузки wget: {result.stderr}")
            raise Exception(f"Ошибка загрузки URL: {result.stderr}")
    except subprocess.TimeoutExpired:
        logger.error("❌ Время загрузки истекло")
        raise Exception("Время загрузки истекло")
    except Exception as e:
        logger.error(f"❌ Ошибка при загрузке: {e}")
        raise Exception(f"Ошибка при загрузке: {e}")


def save_base64_to_file(base64_data, temp_dir, output_filename):
    """Функция для сохранения данных Base64 в файл"""
    try:
        # Декодирование строки Base64
        decoded_data = base64.b64decode(base64_data)

        # Создание директории, если она не существует
        os.makedirs(temp_dir, exist_ok=True)

        # Сохранение в файл
        file_path = os.path.abspath(os.path.join(temp_dir, output_filename))
        with open(file_path, 'wb') as f:
            f.write(decoded_data)

        logger.info(f"✅ Base64 данные сохранены в файл: '{file_path}'")
        return file_path
    except (binascii.Error, ValueError) as e:
        logger.error(f"❌ Ошибка декодирования Base64: {e}")
        raise Exception(f"Ошибка декодирования Base64: {e}")


def process_input(input_data, temp_dir, output_filename, input_type):
    """Функция для обработки входных данных и возврата пути к файлу"""
    if input_type == "path":
        # Если это путь, возвращаем его как есть
        logger.info(f"📁 Обработка пути: {input_data}")
        return input_data
    elif input_type == "url":
        # Если это URL, загружаем файл
        logger.info(f"🌐 Обработка URL: {input_data}")
        os.makedirs(temp_dir, exist_ok=True)
        file_path = os.path.abspath(os.path.join(temp_dir, output_filename))
        return download_file_from_url(input_data, file_path)
    elif input_type == "base64":
        # Если это Base64, декодируем и сохраняем
        logger.info("🔢 Обработка Base64")
        return save_base64_to_file(input_data, temp_dir, output_filename)
    else:
        raise Exception(f"Неподдерживаемый тип ввода: {input_type}")


def queue_prompt(prompt, input_type="image", person_count="single"):
    url = f"http://{server_address}:8188/prompt"
    logger.info(f"Queueing prompt to: {url}")
    p = {"prompt": prompt, "client_id": client_id}
    data = json.dumps(p).encode('utf-8')

    # Логирование содержимого воркфлоу для отладки
    logger.info(f"Количество нодов воркфлоу: {len(prompt)}")
    if input_type == "image":
        prompt_284 = (
            prompt.get('284', {})
            .get('inputs', {})
            .get('image', 'NOT_FOUND')
        )
        logger.info(f"Настройка нода изображения (284): {prompt_284}")
    else:
        prompt_228 = (
            prompt.get('228', {})
            .get('inputs', {})
            .get('video', 'NOT_FOUND')
        )
        logger.info(f"Настройка нода видео (228): {prompt_228}")
    prompt_125 = (
        prompt.get('125', {})
        .get('inputs', {})
        .get('audio', 'NOT_FOUND')
    )
    logger.info(f"Настройка нода аудио (125): {prompt_125}")
    prompt_241 = (
        prompt.get('241', {})
        .get('inputs', {})
        .get('positive_prompt', 'NOT_FOUND')
    )
    logger.info(f"Настройка нода текста (241): {prompt_241}")
    if person_count == "multi":
        if "307" in prompt:
            prompt_307 = (
                prompt.get('307', {})
                .get('inputs', {})
                .get('audio', 'NOT_FOUND')
            )
            logger.info(
                f"Настройка второго нода аудио (307): {prompt_307}"
            )
        elif "313" in prompt:
            prompt_313 = (
                prompt.get('313', {})
                .get('inputs', {})
                .get('audio', 'NOT_FOUND')
            )
            logger.info(
                f"Настройка второго нода аудио (313): {prompt_313}"
            )

    req = urllib.request.Request(url, data=data)
    req.add_header('Content-Type', 'application/json')

    try:
        response = urllib.request.urlopen(req)
        result = json.loads(response.read())
        logger.info(f"Промпт успешно отправлен: {result}")
        return result
    except urllib.error.HTTPError as e:
        logger.error(f"HTTP ошибка: {e.code} - {e.reason}")
        logger.error(f"Содержимое ответа: {e.read().decode('utf-8')}")
        raise
    except Exception as e:
        logger.error(f"Ошибка при отправке промпта: {e}")
        raise


def get_image(filename, subfolder, folder_type):
    url = f"http://{server_address}:8188/view"
    logger.info(f"Getting image from: {url}")
    data = {"filename": filename, "subfolder": subfolder, "type": folder_type}
    url_values = urllib.parse.urlencode(data)
    with urllib.request.urlopen(f"{url}?{url_values}") as response:
        return response.read()


def get_history(prompt_id):
    url = f"http://{server_address}:8188/history/{prompt_id}"
    logger.info(f"Getting history from: {url}")
    with urllib.request.urlopen(url) as response:
        return json.loads(response.read())


def get_videos(ws, prompt, input_type="image", person_count="single"):
    prompt_id = queue_prompt(prompt, input_type, person_count)['prompt_id']
    output_videos = {}
    while True:
        out = ws.recv()
        if isinstance(out, str):
            message = json.loads(out)
            if message['type'] == 'executing':
                data = message['data']
                if data['node'] is None and data['prompt_id'] == prompt_id:
                    break
        else:
            continue

    history = get_history(prompt_id)[prompt_id]
    for node_id in history['outputs']:
        node_output = history['outputs'][node_id]
        videos_output = []
        if 'gifs' in node_output:
            for video in node_output['gifs']:
                # Чтение файла напрямую через fullpath и кодирование в base64
                with open(video['fullpath'], 'rb') as f:
                    video_data = base64.b64encode(f.read()).decode('utf-8')
                videos_output.append(video_data)
        output_videos[node_id] = videos_output

    return output_videos


def load_workflow(workflow_path):
    with open(workflow_path, 'r') as file:
        return json.load(file)


def get_workflow_path(input_type, person_count):
    """Возвращает путь к файлу воркфлоу в \
    зависимости от input_type и person_count"""
    if input_type == "image":
        if person_count == "single":
            return "/I2V_single.json"
        else:  # multi
            return "/I2V_multi.json"
    else:  # video
        if person_count == "single":
            return "/V2V_single.json"
        else:  # multi
            return "/V2V_multi.json"


def get_audio_duration(audio_path):
    """Возвращает длительность аудиофайла в секундах"""
    try:
        duration = librosa.get_duration(path=audio_path)
        return duration
    except Exception as e:
        logger.warning(
            f"Не удалось рассчитать длительность аудио ({audio_path}): {e}"
        )
        return None


def calculate_max_frames_from_audio(wav_path, wav_path_2=None, fps=25):
    """Вычисляет max_frames на основе длительности аудио"""
    durations = []

    # Расчет длительности первого аудио
    duration1 = get_audio_duration(wav_path)
    if duration1 is not None:
        durations.append(duration1)
        logger.info(f"Длительность первого аудио: {duration1:.2f} сек")

    # Расчет длительности второго аудио (для multi person)
    if wav_path_2:
        duration2 = get_audio_duration(wav_path_2)
        if duration2 is not None:
            durations.append(duration2)
            logger.info(f"Длительность второго аудио: {duration2:.2f} сек")

    if not durations:
        logger.warning(
            "Невозможно рассчитать длительность аудио. \
                Используется значение по умолчанию 81."
        )
        return 81

    # Расчет max_frames на основе самого длинного аудио
    max_duration = max(durations)
    max_frames = int(max_duration * fps) + 81

    logger.info(
        f"Максимальная длительность аудио: {max_duration:.2f} сек, \
            вычислено max_frames: {max_frames}"
    )
    return max_frames


def process_with_multi_gpu(job_input, task_id, input_type, person_count,
                           media_path, wav_path, wav_path_2, prompt_text,
                           width, height, max_frame, gpu_count):
    """Основная функция для мульти-GPU обработки"""
    try:
        # Создаем временную директорию для задачи
        temp_dir = os.path.abspath(task_id)
        os.makedirs(temp_dir, exist_ok=True)

        # Выбираем workflow
        if input_type == "image":
            if person_count == "single":
                workflow_path = "I2V_single.json"
            else:
                workflow_path = "I2V_multi.json"
        else:
            if person_count == "single":
                workflow_path = "V2V_single.json"
            else:
                workflow_path = "V2V_multi.json"

        # Загружаем базовый workflow
        base_workflow = load_workflow(workflow_path)

        # Настраиваем общие параметры
        base_workflow["125"]["inputs"]["audio"] = wav_path
        base_workflow["241"]["inputs"]["positive_prompt"] = prompt_text
        base_workflow["245"]["inputs"]["value"] = width
        base_workflow["246"]["inputs"]["value"] = height

        if input_type == "image":
            base_workflow["284"]["inputs"]["image"] = media_path
        else:
            base_workflow["228"]["inputs"]["video"] = media_path

        # Настраиваем второй аудио для multi person
        if person_count == "multi":
            if input_type == "image":
                if "307" in base_workflow:
                    base_workflow["307"]["inputs"]["audio"] = wav_path_2
            else:
                if "313" in base_workflow:
                    base_workflow["313"]["inputs"]["audio"] = wav_path_2

        # Вычисляем сегменты
        segments = calculate_segments(max_frame, gpu_count)
        logger.info(f"Создано {len(segments)} сегментов для обработки")

        # Создаем workflow файлы для каждого сегмента
        segment_workflows = []
        for segment in segments:
            workflow_file = create_segment_workflow(
                base_workflow,
                segment,
                temp_dir
            )
            segment_workflows.append((segment, workflow_file))

        # Запускаем параллельную обработку сегментов
        segment_results = []
        with ThreadPoolExecutor(max_workers=gpu_count) as executor:
            # Отправляем задачи на каждый GPU
            future_to_segment = {
                executor.submit(
                    process_segment_on_gpu,
                    segment_info,
                    workflow_file,
                    temp_dir,
                    segment_info['gpu_id']
                ): segment_info for segment_info, workflow_file
                in segment_workflows
            }

            # Собираем результаты
            for future in as_completed(future_to_segment):
                segment_info = future_to_segment[future]
                try:
                    result = future.result()
                    if result:
                        segment_results.append((segment_info, result))
                        logger.info(
                            f"Сегмент {segment_info['segment_id']} \
                                обработан успешно"
                        )
                    else:
                        logger.error(
                            f"Ошибка обработки сегмента \
                                {segment_info['segment_id']}"
                        )
                except Exception as e:
                    logger.error(
                        f"Исключение при обработке сегмента \
                            {segment_info['segment_id']}: {e}"
                    )

        if not segment_results:
            return {"error": "Не удалось обработать ни одного сегмента"}

        # Сортируем результаты по segment_id
        segment_results.sort(key=lambda x: x[0]['segment_id'])

        # Извлекаем пути к видео файлам
        segment_video_files = []
        for segment_info, result in segment_results:
            if result and len(result) > 0:
                video_path = result[0]['fullpath']
                segment_video_files.append(video_path)
                logger.info(
                    f"Сегмент {segment_info['segment_id']}: {video_path}"
                )

        if not segment_video_files:
            return {"error": "Не найдено видео файлов для склеивания"}

        # Склеиваем все сегменты
        final_output_path = os.path.join(temp_dir, "final_video.mp4")
        merge_video_segments(segment_video_files, final_output_path, temp_dir)

        # Читаем финальное видео и кодируем в base64
        with open(final_output_path, 'rb') as f:
            video_data = base64.b64encode(f.read()).decode('utf-8')

        # Очищаем временные файлы
        shutil.rmtree(temp_dir, ignore_errors=True)

        logger.info(
            f"Мульти-GPU обработка завершена успешно. \
                GPU использовано: {gpu_count}"
        )

        return {
            "success": True,
            "video": video_data,
            "gpu_count": gpu_count,
            "segments_processed": len(segment_results),
            "message": f"Видео успешно создано с {gpu_count} GPU"
        }

    except Exception as e:
        logger.error(f"Ошибка мульти-GPU обработки: {e}")
        return {"error": f"Ошибка мульти-GPU обработки: {str(e)}"}


def handler(job):
    job_input = job.get("input", {})

    logger.info(f"Получены входные данные задачи: {job_input}")
    task_id = f"task_{uuid.uuid4()}"

    # Проверяем доступные GPU
    gpu_count = get_available_gpus()
    use_multi_gpu = gpu_count > 1 and job_input.get("use_multi_gpu", True)

    if use_multi_gpu:
        logger.info(f"Используем мульти-GPU режим с {gpu_count} GPU")
    else:
        logger.info("Используем однопоточный режим")

    # Определение типа ввода и количества персон
    input_type = job_input.get("input_type", "image")  # image/video
    person_count = job_input.get("person_count", "single")  # single/multi

    logger.info(
        f"Тип воркфлоу: {input_type}, количество персон: {person_count}"
    )

    # Определение пути к файлу воркфлоу
    workflow_path = get_workflow_path(input_type, person_count)
    logger.info(f"Используемый воркфлоу: {workflow_path}")

    # Обработка входного изображения/видео
    media_path = None
    if input_type == "image":
        # Обработка входного изображения
        # (используется один из: image_path, image_url, image_base64)
        if "image_path" in job_input:
            media_path = process_input(
                job_input["image_path"],
                task_id,
                "input_image.jpg",
                "path"
            )
        elif "image_url" in job_input:
            media_path = process_input(
                job_input["image_url"],
                task_id,
                "input_image.jpg",
                "url"
            )
        elif "image_base64" in job_input:
            media_path = process_input(
                job_input["image_base64"],
                task_id,
                "input_image.jpg",
                "base64"
            )
        else:
            # Использование значения по умолчанию
            media_path = "/examples/image.jpg"
            logger.info(
                "Используется изображение по умолчанию: /examples/image.jpg"
            )
    else:  # video
        # Обработка входного видео
        # (используется один из: video_path, video_url, video_base64)
        if "video_path" in job_input:
            media_path = process_input(
                job_input["video_path"],
                task_id,
                "input_video.mp4",
                "path"
            )
        elif "video_url" in job_input:
            media_path = process_input(
                job_input["video_url"],
                task_id,
                "input_video.mp4",
                "url"
            )
        elif "video_base64" in job_input:
            media_path = process_input(
                job_input["video_base64"],
                task_id,
                "input_video.mp4",
                "base64"
            )
        else:
            # Использование значения по умолчанию
            # (если видео нет, используется изображение по умолчанию)
            media_path = "/examples/image.jpg"
            logger.info(
                "Используется изображение по умолчанию: /examples/image.jpg"
            )

    # Обработка входного аудио
    # (используется один из: wav_path, wav_url, wav_base64)
    wav_path = None
    wav_path_2 = None  # Второе аудио для multi person

    if "wav_path" in job_input:
        wav_path = process_input(
            job_input["wav_path"],
            task_id,
            "input_audio.wav", "path"
        )
    elif "wav_url" in job_input:
        wav_path = process_input(
            job_input["wav_url"],
            task_id,
            "input_audio.wav",
            "url"
        )
    elif "wav_base64" in job_input:
        wav_path = process_input(
            job_input["wav_base64"],
            task_id,
            "input_audio.wav",
            "base64"
        )
    else:
        # Использование значения по умолчанию
        wav_path = "/examples/audio.mp3"
        logger.info("Используется аудио по умолчанию: /examples/audio.mp3")

    # Обработка второго аудио для multi person
    if person_count == "multi":
        if "wav_path_2" in job_input:
            wav_path_2 = process_input(
                job_input["wav_path_2"],
                task_id,
                "input_audio_2.wav",
                "path"
            )
        elif "wav_url_2" in job_input:
            wav_path_2 = process_input(
                job_input["wav_url_2"],
                task_id, "input_audio_2.wav",
                "url"
            )
        elif "wav_base64_2" in job_input:
            wav_path_2 = process_input(
                job_input["wav_base64_2"],
                task_id,
                "input_audio_2.wav",
                "base64"
            )
        else:
            # Использование значения по умолчанию (такое же, как первое аудио)
            wav_path_2 = wav_path
            logger.info("Второе аудио отсутствует, используется первое аудио.")

    # Проверка обязательных полей и установка значений по умолчанию
    prompt_text = job_input.get("prompt", "A person talking naturally")
    width = job_input.get("width", 512)
    height = job_input.get("height", 512)

    # Настройка max_frame
    # (если не указано, автоматически на основе длительности аудио)
    max_frame = job_input.get("max_frame")
    if max_frame is None:
        logger.info(
            "max_frame не указан. \
            Выполняется автоматический расчет на основе длительности аудио."
        )
        max_frame = calculate_max_frames_from_audio(
            wav_path, wav_path_2 if person_count == "multi" else None
        )
    else:
        logger.info(f"Пользовательский max_frame: {max_frame}")

    logger.info(
        f"Настройки воркфлоу: prompt='{prompt_text}', \
            width={width}, height={height}, max_frame={max_frame}"
    )
    logger.info(f"Путь к медиа: {media_path}")

    # Если используем мульти-GPU, переходим к параллельной обработке
    if use_multi_gpu:
        return process_with_multi_gpu(
            job_input, task_id, input_type, person_count,
            media_path, wav_path, wav_path_2, prompt_text,
            width, height, max_frame, gpu_count
        )
    logger.info(f"Путь к аудио: {wav_path}")
    if person_count == "multi":
        logger.info(f"Путь ко второму аудио: {wav_path_2}")

    prompt = load_workflow(workflow_path)

    # Проверка наличия файлов
    if not os.path.exists(media_path):
        logger.error(f"Медиафайл не существует: {media_path}")
        return {"error": f"Не удалось найти медиафайл: {media_path}"}

    if not os.path.exists(wav_path):
        logger.error(f"Аудиофайл не существует: {wav_path}")
        return {"error": f"Не удалось найти аудиофайл: {wav_path}"}

    if person_count == "multi" and wav_path_2 \
            and not os.path.exists(wav_path_2):

        logger.error(f"Второй аудиофайл не существует: {wav_path_2}")
        return {"error": f"Не удалось найти второй аудиофайл: {wav_path_2}"}

    logger.info(f"Размер медиафайла: {os.path.getsize(media_path)} байт")
    logger.info(f"Размер аудиофайла: {os.path.getsize(wav_path)} байт")
    if person_count == "multi" and wav_path_2:
        logger.info(
            f"Размер второго аудиофайла: {os.path.getsize(wav_path_2)} байт"
        )

    # Настройка нодов воркфлоу
    if input_type == "image":
        # Воркфлоу I2V: настройка входного изображения
        prompt["284"]["inputs"]["image"] = media_path
    else:
        # Воркфлоу V2V: настройка входного видео
        prompt["228"]["inputs"]["video"] = media_path

    # Общие настройки
    prompt["125"]["inputs"]["audio"] = wav_path
    prompt["241"]["inputs"]["positive_prompt"] = prompt_text
    prompt["245"]["inputs"]["value"] = width
    prompt["246"]["inputs"]["value"] = height

    prompt["270"]["inputs"]["value"] = max_frame

    # Настройка второго аудио для multi person
    if person_count == "multi":
        # Настройка второго аудио нода в зависимости от типа воркфлоу
        if input_type == "image":  # Для I2V_multi.json
            if "307" in prompt:
                prompt["307"]["inputs"]["audio"] = wav_path_2
        else:  # Для V2V_multi.json
            if "313" in prompt:
                prompt["313"]["inputs"]["audio"] = wav_path_2

    ws_url = f"ws://{server_address}:8188/ws?clientId={client_id}"
    logger.info(f"Подключение к WebSocket: {ws_url}")

    # Сначала проверяем возможность HTTP соединения
    http_url = f"http://{server_address}:8188/"
    logger.info(f"Проверка HTTP соединения: {http_url}")

    # Проверка HTTP соединения (максимум 3 минуты)
    max_http_attempts = 180
    for http_attempt in range(max_http_attempts):
        try:
            import urllib.request
            urllib.request.urlopen(http_url, timeout=5)
            logger.info(
                f"HTTP соединение установлено (попытка {http_attempt+1})"
            )
            break
        except Exception as e:
            logger.warning(
                f"HTTP соединение не удалось \
                    (попытка {http_attempt+1}/{max_http_attempts}): {e}"
            )
            if http_attempt == max_http_attempts - 1:
                raise Exception(
                    "Не удалось подключиться к серверу ComfyUI. \
                        Проверьте, запущен ли сервер."
                )
            time.sleep(1)

    ws = websocket.WebSocket()
    # Попытка подключения WebSocket (максимум 3 минуты)
    max_attempts = int(180/5)  # 3 минуты (попытка каждые 5 секунд)
    for attempt in range(max_attempts):
        try:
            ws.connect(ws_url)
            logger.info(
                f"WebSocket соединение установлено (попытка {attempt+1})"
            )
            break
        except Exception as e:
            logger.warning(
                f"WebSocket соединение не удалось \
                    (попытка {attempt+1}/{max_attempts}): {e}"
            )
            if attempt == max_attempts - 1:
                raise Exception(
                    "Время ожидания WebSocket \
                        соединения истекло (3 минуты)"
                )
            time.sleep(5)
    videos = get_videos(ws, prompt, input_type, person_count)
    ws.close()

    # Обработка случая, когда видео не найдено
    for node_id in videos:
        if videos[node_id]:
            return {"video": videos[node_id][0]}

    return {"error": "Видео не найдено."}


runpod.serverless.start({"handler": handler})

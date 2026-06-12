import whisper
from whisper.utils import get_writer
import os

# 1. تحميل الموديل (Large-v3 هو الأفضل للمحتوى الطبي المختلط)
model = whisper.load_model("large-v3")

input_video = "lec2.mp4" # تأكد من تغيير الاسم لاسم ملفك
output_directory = "." 

# 2. تنفيذ عملية التفريغ
# الـ Prompt ضروري جداً هنا عشان المصطلحات الطبية تطلع صح
result = model.transcribe(
    input_video,
    task="transcribe",
    initial_prompt="هذا الفيديو يحتوي على مصطلحات طبية مثل Medicine, Cardiology, Clinic, Patient.",
    language="ar"
)

# 3. حفظ النتيجة بصيغة SRT المناسبة لليوتيوب
writer = get_writer("srt", output_directory)
writer(result, input_video)

print("مبروك! ملف الـ SRT جاهز الآن بجانب الفيديو.")
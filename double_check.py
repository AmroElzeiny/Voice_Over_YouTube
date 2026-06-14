import whisper
from openai import OpenAI
from datetime import timedelta
import os
from pathlib import Path

from dotenv import load_dotenv

# 1. إعدادات OpenAI
load_dotenv(Path(__file__).resolve().parent / "AI_voice_over_system" / ".env")
client = OpenAI()

def format_timestamp(seconds):
    td = str(timedelta(seconds=seconds))
    if '.' in td:
        # تحويل 0:00:00.000 إلى 00:00:00,000
        parts = td.split('.')
        time_part = parts[0].zfill(8)
        ms_part = parts[1][:3].ljust(3, '0')
        return f"{time_part},{ms_part}"
    return td.zfill(8) + ",000"

def process_batch_with_ai(batch_texts):
    # تجميع الجمل في نص واحد مع فواصل واضحة
    combined_input = "\n---\n".join(batch_texts)
    
    prompt = """أنت خبير في المصطلحات الطبية (عربي/إنجليزي). 
    سأعطيك قائمة من الجمل الطبية المفصولة بـ '---'. 
    قم بتصحيح المصطلحات الطبية والإملاء مع الحفاظ على الكلمات الإنجليزية كما هي (Medical terms).
    مهم جداً: أعد لي الجمل بنفس الترتيب وبنفس العدد، وافصل بينها بـ '---' فقط."""

    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": prompt},
            {"role": "user", "content": combined_input}
        ],
        temperature=0 # لضمان الدقة وعدم التأليف
    )
    
    # تحويل الرد مرة أخرى لقائمة جمل
    corrected_batch = response.choices[0].message.content.split("\n---\n")
    return [s.strip() for s in corrected_batch]

# 2. تشغيل Whisper
print("بدء استخراج النصوص من الفيديو (Whisper)...")
model = whisper.load_model("medium") 
result = model.transcribe("video.mp4", language="ar")

# 3. معالجة الجمل بنظام المجموعات (Batching) لتوفير التوكينز
all_segments = result['segments']
refined_segments = []
batch_size = 25 # معالجة 25 جملة في كل طلب لـ OpenAI

print(f"بدء التدقيق الطبي لـ {len(all_segments)} جملة...")

for i in range(0, len(all_segments), batch_size):
    current_batch = all_segments[i:i + batch_size]
    texts_to_correct = [seg['text'].strip() for seg in current_batch]
    
    try:
        print(f"جاري معالجة المجموعة {i//batch_size + 1}...")
        corrected_texts = process_batch_with_ai(texts_to_correct)
        
        # التأكد من أن الـ AI أرجع نفس عدد الجمل
        for j, segment in enumerate(current_batch):
            # إذا فشل الـ AI في إرجاع نفس العدد، نستخدم النص الأصلي كخطة بديلة
            text = corrected_texts[j] if j < len(corrected_texts) else segment['text']
            
            start = format_timestamp(segment['start'])
            end = format_timestamp(segment['end'])
            refined_segments.append(f"{segment['id'] + 1}\n{start} --> {end}\n{text}\n\n")
            
    except Exception as e:
        print(f"خطأ في المجموعة: {e}")
        # في حالة الخطأ، أضف النصوص الأصلية للمحافظة على الملف
        for segment in current_batch:
            start = format_timestamp(segment['start'])
            end = format_timestamp(segment['end'])
            refined_segments.append(f"{segment['id'] + 1}\n{start} --> {end}\n{segment['text']}\n\n")

# 4. حفظ ملف الـ SRT النهائي
with open("refined_medical_subs.srt", "w", encoding="utf-8") as f:
    f.writelines(refined_segments)

print("تم الحفظ بنجاح! ملف refined_medical_subs.srt جاهز.")

import os
from openai import OpenAI
import re

# ==========================================
# إعدادات المستخدم
# ==========================================
API_KEY = "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" # حط مفتاحك هنا
INPUT_FILE = "lec2.srt"      # اسم الملف اللي طالع من Whisper
OUTPUT_FILE = "lec2_hindi.srt"    # اسم الملف الهندي الجديد
BATCH_SIZE = 20                      # عدد الجمل في كل شحنة (للسرعة والتوفير)

client = OpenAI(api_key=API_KEY)

# ==========================================
# الدوال المساعدة
# ==========================================

def parse_srt(filename):
    """قراءة ملف SRT وتقسيمه إلى كتل"""
    with open(filename, 'r', encoding='utf-8') as f:
        content = f.read()
    # تقسيم الملف بناءً على الفراغات بين الكتل
    return re.split(r'\n\s*\n', content.strip())

def translate_batch(texts):
    """إرسال مجموعة نصوص للترجمة دفعة واحدة"""
    # ترقيم الجمل داخل الباتش عشان الموديل يحافظ على الترتيب
    numbered_text = "\n".join([f"{i+1}. {text}" for i, text in enumerate(texts)])
    
    system_prompt = """
    You are an expert medical translator specialized in Hindi.
    Task: Translate the following mixed Arabic/English medical subtitles into formal, accurate Hindi.
    
    Rules:
    1. Translate BOTH Arabic and English parts to Hindi.
    2. For Medical Terms (e.g., 'Hypertension', 'Diagnosis'), use the standard medical Hindi terminology used in Indian hospitals.
    3. Maintain the professional tone.
    4. Return ONLY the translated lines in a numbered list corresponding to the input.
    5. Do NOT include the numbers in the final output lines, just the text.
    """

    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": numbered_text}
            ],
            temperature=0.3 # درجة منخفضة للدقة العلمية
        )
        
        # تنظيف الرد لتحويله لقائمة مرة أخرى
        raw_response = response.choices[0].message.content.strip()
        # إزالة الترقيم الذي قد يضعه الموديل أحياناً (مثل "1. النص")
        translated_lines = []
        for line in raw_response.split('\n'):
            clean_line = re.sub(r'^\d+\.?\s*', '', line).strip()
            if clean_line:
                translated_lines.append(clean_line)
                
        return translated_lines

    except Exception as e:
        print(f"Error translating batch: {e}")
        return texts # في حالة الفشل، أعد النص الأصلي مؤقتاً

# ==========================================
# الكود الرئيسي
# ==========================================

def main():
    print(f"جاري قراءة الملف: {INPUT_FILE}...")
    srt_blocks = parse_srt(INPUT_FILE)
    
    # استخراج النصوص فقط من الكتل
    text_segments = []
    headers = [] # لحفظ التوقيت والأرقام
    
    for block in srt_blocks:
        lines = block.split('\n')
        if len(lines) >= 3:
            headers.append(lines[0] + '\n' + lines[1]) # الرقم + التوقيت
            text_segments.append(" ".join(lines[2:])) # النص (قد يكون أكثر من سطر)
        else:
            # تخطي الكتل التالفة إن وجدت
            continue
            
    total_segments = len(text_segments)
    print(f"تم العثور على {total_segments} جملة. بدء الترجمة...")

    translated_segments = []
    
    # المعالجة بنظام المجموعات (Batches)
    for i in range(0, total_segments, BATCH_SIZE):
        batch = text_segments[i : i + BATCH_SIZE]
        print(f"--> ترجمة المجموعة {i//BATCH_SIZE + 1} من {total_segments//BATCH_SIZE + 1}...")
        
        translations = translate_batch(batch)
        
        # التأكد من تطابق العدد (حالة نادرة لو الـ AI دمج جملتين)
        if len(translations) != len(batch):
            print("تحذير: عدد الجمل المترجمة لا يطابق الأصل في هذه المجموعة. سيتم استخدام الأصل كاحتياطي.")
            translated_segments.extend(batch)
        else:
            translated_segments.extend(translations)

    # بناء الملف الجديد
    print("جاري حفظ الملف الهندي...")
    with open(OUTPUT_FILE, 'w', encoding='utf-8') as f:
        for i in range(len(headers)):
            if i < len(translated_segments):
                f.write(f"{headers[i]}\n{translated_segments[i]}\n\n")

    print(f"تمت المهمة! الملف جاهز: {OUTPUT_FILE}")

if __name__ == "__main__":
    main()
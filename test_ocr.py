from PIL import Image, ImageEnhance, ImageFilter
import pytesseract

image = Image.open("test_image.png").convert("RGB")
image = image.resize((image.width * 2, image.height * 2), Image.LANCZOS)
image = image.filter(ImageFilter.SHARPEN)
enhancer = ImageEnhance.Contrast(image)
image = enhancer.enhance(1.5)
text = pytesseract.image_to_string(image, lang="eng", config="--psm 6 --oem 3 -c preserve_interword_spaces=1").strip()
print(text)
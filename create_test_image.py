from PIL import Image, ImageDraw, ImageFont
import os

# Create high-resolution image (300 DPI equivalent)
width, height = 600, 300  # 3x larger for 300 DPI at 200x100 effective size
image = Image.new("RGB", (width, height), color="white")
draw = ImageDraw.Draw(image)

# Use DejaVuSans font
try:
    font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 60)
except:
    font = ImageFont.load_default()

# Draw text
draw.text((30, 120), "Hello World", fill="black", font=font)

# Save image
output_path = "/home/pdfconverterai-api/htdocs/api.pdfconverterai.com/test_image_dejavu.png"
image.save(output_path, dpi=(300, 300))
print(f"Image saved at {output_path}")

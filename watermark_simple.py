import cv2
import numpy as np

def remove_watermark_simple(input_path, output_path):
    """
    Remove marca d'água de vídeo usando OpenCV (inpainting).
    A marca d'água deve estar em uma posição fixa.
    """
    cap = cv2.VideoCapture(input_path)
    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    fps = cap.get(cv2.CAP_PROP_FPS)

    fourcc = cv2.VideoWriter_fourcc(*'mp4v')
    out = cv2.VideoWriter(output_path, fourcc, fps, (width, height))

    # Define a máscara da marca d'água (exemplo: canto inferior direito)
    mask = np.zeros((height, width), dtype=np.uint8)
    mask[height-100:height, width-200:width] = 255  # ajuste conforme necessário

    while cap.isOpened():
        ret, frame = cap.read()
        if not ret:
            break
        inpainted = cv2.inpaint(frame, mask, 3, cv2.INPAINT_TELEA)
        out.write(inpainted)

    cap.release()
    out.release()
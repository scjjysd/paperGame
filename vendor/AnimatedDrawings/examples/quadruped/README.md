# How to use Quadruped Extension

This will teach you how to animate your own drawing and configure it for quadruped animation

## Run image_to_annotation.py

First use the image_to_annotation.py on your drawing. You can specify the path to your file and output as well.

```bash
python image_to_animation.py path/to/your/file 
```

## Usage

Please put the human_to_animal.py in the same directory or folder that is newly generated from the previous command. Use human_to_animal.py on the newly produced char_cfg.yaml

```python
python human_to_animal path/to/your/yaml
```
Please then edit the file four_example.yaml and set the character_cfg as the path to your new animal config file
```yaml
scene:
  ANIMATED_CHARACTERS:
    - character_cfg: path/to/your/new/animal/config
      motion_cfg: examples/config/motion/zombie.yaml
      retarget_cfg: examples/config/retarget/four_legs.yaml
controller:
  MODE: video_render
  OUTPUT_VIDEO_PATH: ./video.gif
```
You can then run the animation using four_example.yaml file using this command
```python
from animated_drawings import render
render.start('./examples/config/mvc/four_legs.yaml')
```

## Notes

Please make sure that human_to_animal.py is either in the same folder or a folder path is specified within input

Please note that in order for the best quadrupedal experience, you may have to edit the bounding box configurations found inside the image_to_animal.py  
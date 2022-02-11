import os

import torch.utils.data
import torchvision
import tqdm

import datasets
import utils

if __name__ == '__main__':
    # Load cfg and create components builder
    cfg = utils.builder.load_cfg()
    builder = utils.builder.Builder(cfg)

    # Device
    device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')

    # 1. Dataset
    valset, valloader = builder.build_dataset('val')

    # 2. Model
    model = builder.build_model(valset.num_classes, pretrained=True).to(device)
    model.eval()
    model_name = cfg['model']['name']
    amp_enabled = cfg['model']['amp_enabled']
    print(f'Activated model: {model_name}')

    # Load image names
    image_names = []
    for image_path in valset.images:
        image_name = image_path.replace('\\', '/').split('/')[-1]
        image_names.append(image_name)

    # Select outputing groundtruth
    gt = input('Do you also output groundtruth ([y]/n)? ')
    if gt == 'n':
        gt = False
    else:
        gt = True

    # Save segmentation results
    step = 0
    result_dir = os.path.join('demo', model_name.lower())
    groundtruth_dir = os.path.join('demo', 'groundtruth')
    os.makedirs(result_dir, exist_ok=True)
    os.makedirs(groundtruth_dir, exist_ok=True)
    for images, targets in tqdm.tqdm(valloader, desc='Demo'):
        images, targets = images.to(device), targets.to(device)

        with torch.cuda.amp.autocast(enabled=amp_enabled):
            with torch.no_grad():
                outputs = model(images)
                outputs = torch.argmax(outputs, dim=1)

        if gt:
            targets = datasets.utils.decode_segmap_to_color_image(targets, valset.colors, valset.num_classes,
                                                                  valset.ignore_index, valset.ignore_color)
        outputs = datasets.utils.decode_segmap_to_color_image(outputs, valset.colors, valset.num_classes)

        # process per 1 batch
        for i in range(targets.shape[0]):
            if gt:
                torchvision.utils.save_image(targets[i], os.path.join(groundtruth_dir, image_names[step]))
            torchvision.utils.save_image(outputs[i], os.path.join(result_dir, image_names[step]))
            step += 1

#!/usr/bin/env python3
# This file is covered by the LICENSE file in the root of this project.

import os
import imp
import time
import numpy as np

import torch
import torch.nn as nn
import torch.optim as optim
import torch.backends.cudnn as cudnn
import __init__ as booger

from tqdm import tqdm
from modules.KNN import KNN

# from modules.SalsaNextWithMotionAttention import *
from modules.MFMOS import *

# from modules.PointRefine.spvcnn import SPVCNN

# from modules.PointRefine.spvcnn_lite import SPVCNN
import open3d as o3d

from modules.loss.custom_loss import loss_and_pred


class User:
    def __init__(
        self,
        ARCH,
        DATA,
        datadir,
        outputdir,
        modeldir,
        split,
        point_refine=False,
        save_movable=False,
    ):
        # parameters
        self.ARCH = ARCH
        self.DATA = DATA
        self.datadir = datadir
        self.outputdir = outputdir
        self.modeldir = modeldir
        self.split = split
        self.post = None
        self.infer_batch_size = 1
        self.point_refine = point_refine
        self.save_movable = save_movable
        # get the data
        parserModule = imp.load_source(
            "parserModule",
            f"{booger.TRAIN_PATH}/common/dataset/{self.DATA['name']}/parser.py",
        )
        self.parser = parserModule.Parser(
            root=self.datadir,
            train_sequences=self.DATA["split"]["train"],
            valid_sequences=self.DATA["split"]["valid"],
            test_sequences=self.DATA["split"]["test"],
            split=self.split,
            labels=self.DATA["labels"],
            residual_aug=self.ARCH["train"]["residual_aug"],
            valid_residual_delta_t=self.ARCH["train"]["valid_residual_delta_t"],
            color_map=self.DATA["color_map"],
            learning_map=self.DATA["moving_learning_map"],
            movable_learning_map=self.DATA["movable_learning_map"],
            learning_map_inv=self.DATA["moving_learning_map_inv"],
            movable_learning_map_inv=self.DATA["movable_learning_map_inv"],
            sensor=self.ARCH["dataset"]["sensor"],
            max_points=self.ARCH["dataset"]["max_points"],
            batch_size=self.infer_batch_size,
            workers=8,  # self.ARCH["train"]["workers"],
            gt=True,
            shuffle_train=False,
        )

        with torch.no_grad():
            torch.nn.Module.dump_patches = True

            # 모델 초기화 및 DataParallel 래핑
            self.model = MFMOS(
                nclasses=self.parser.get_n_classes(),
                movable_nclasses=self.parser.get_n_classes(movable=True),
                params=ARCH,
                num_batch=self.infer_batch_size,
            )
            self.model = nn.DataParallel(self.model)

            checkpoint = "MFMOS_train_best"
            w_dict = torch.load(
                f"{self.modeldir}/{checkpoint}",
                map_location=lambda storage, loc: storage,
            )

            # 체크포인트의 state_dict 키에 'module.' 접두어가 없는 경우 붙여주기
            state_dict = w_dict["state_dict"]
            new_state_dict = {}
            for k, v in state_dict.items():
                # 만약 키가 이미 'module.'로 시작하지 않는다면 접두어 추가
                if not k.startswith("module."):
                    new_state_dict[f"module.{k}"] = v
                else:
                    new_state_dict[k] = v

            # 수정된 state_dict로 모델에 로드
            self.model.load_state_dict(new_state_dict, strict=True)

            self.set_knn_post()

        self.set_gpu_cuda()

    def set_knn_post(self):
        # use knn post processing?
        if self.ARCH["post"]["KNN"]["use"]:
            self.post = KNN(
                self.ARCH["post"]["KNN"]["params"], self.parser.get_n_classes()
            )

    def set_gpu_cuda(self):
        # GPU?
        self.gpu = False
        self.model_single = self.model
        self.device = torch.device(
            "cuda" if torch.cuda.is_available() else "cpu")
        print("Infering in device: ", self.device)
        if torch.cuda.is_available() and torch.cuda.device_count() > 0:
            cudnn.benchmark = True
            cudnn.fastest = True
            self.gpu = True
            self.model.cuda()
            if self.point_refine:
                self.refine_module.cuda()

    def infer(self):
        cnn, knn = [], []

        if self.split == "valid":
            self.infer_subset(
                loader=self.parser.get_valid_set(),
                to_orig_fn=self.parser.to_original,
                cnn=cnn,
                knn=knn,
            )
        elif self.split == "train":
            self.infer_subset(
                loader=self.parser.get_train_set(),
                to_orig_fn=self.parser.to_original,
                cnn=cnn,
                knn=knn,
            )
        elif self.split == "test":
            self.infer_subset(
                loader=self.parser.get_test_set(),
                to_orig_fn=self.parser.to_original,
                cnn=cnn,
                knn=knn,
            )
        elif self.split == None:
            self.infer_subset(
                loader=self.parser.get_train_set(),
                to_orig_fn=self.parser.to_original,
                cnn=cnn,
                knn=knn,
            )
            # do valid set
            self.infer_subset(
                loader=self.parser.get_valid_set(),
                to_orig_fn=self.parser.to_original,
                cnn=cnn,
                knn=knn,
            )
            # do test set
            self.infer_subset(
                loader=self.parser.get_test_set(),
                to_orig_fn=self.parser.to_original,
                cnn=cnn,
                knn=knn,
            )
        else:
            raise NotImplementedError

        print(
            f"Mean CNN inference time:{'%.8f'%np.mean(cnn)}\t std:{'%.8f'%np.std(cnn)}"
        )
        # print(
        #     f"Mean KNN inference time:{'%.8f'%np.mean(knn)}\t std:{'%.8f'%np.std(knn)}"
        # )
        print(f"Total Frames: {len(cnn)}")
        print("Finished Infering")

        return

    def infer_subset(self, loader, to_orig_fn, cnn, knn):

        # 평가 모드로 전환
        self.model.eval()

        # GPU 사용 시 캐시 비우기
        if self.gpu:
            torch.cuda.empty_cache()

        with torch.no_grad():
            for i, (
                GTs_moving,  # (150000, )
                GTs_movable,  # (150000, )
                proj_full,  # (13, h, w)
                proj_x,  # (150000, )
                proj_y,  # (150000, )
                path_seq,  # str
                path_name,  # str
                npoints,  # int
                (bev_f1, bev_f2, bev_f3), # (64, 313, 313), (128, 157, 157), (256, 79, 79)
            ) in enumerate(tqdm(loader, ncols=80)):
                if self.gpu:
                    GTs_moving =  GTs_moving.cuda(non_blocking=True)
                    GTs_movable = GTs_movable.cuda(non_blocking=True)
                    proj_full = proj_full.cuda(non_blocking=True)
                    proj_x =   proj_x.cuda(non_blocking=True)
                    proj_y =   proj_y.cuda(non_blocking=True)
                    bev_f1 = bev_f1.cuda(non_blocking=True)
                    bev_f2 = bev_f2.cuda(non_blocking=True)
                    bev_f3 = bev_f3.cuda(non_blocking=True)

                start = time.time()
                output, movable_output = self.model(proj_full, bev_f1, bev_f2, bev_f3)
                cnn.append(time.time() - start)

                bs = npoints.shape[0]
                """""" """""" """""" """""" """""" """"""
                """   모든것을 batch 단위 간주   """
                """""" """""" """""" """""" """""" """"""

                all_moving, all_movable, all_GTs_moving, all_GTs_movable = (
                    [],
                    [],
                    [],
                    [],
                )
                for b in range(bs):
                    n = npoints[b]
                    moving_single = output[b]  # (3, h, w)
                    movable_single = movable_output[b]  # (3, h, w)
                    proj_y_single = proj_y[b][:n]  # (#points, )
                    proj_x_single = proj_x[b][:n]  # (#points, )
                    GTs_moving_single = GTs_moving[b][:n]  # (#points, )
                    GTs_movable_single = GTs_movable[b][:n]  # (#points, )

                    moving_single = moving_single[
                        :, proj_y_single, proj_x_single
                    ]  # (3, #points)
                    movable_single = movable_single[
                        :, proj_y_single, proj_x_single
                    ]  # (3, #points)

                    all_moving.append(moving_single)
                    all_movable.append(movable_single)
                    all_GTs_moving.append(GTs_moving_single)
                    all_GTs_movable.append(GTs_movable_single)

                moving = torch.cat(all_moving, dim=1)  # (3, sum(npoints))
                movable = torch.cat(all_movable, dim=1)  # (3, sum(npoints))
                GTs_moving = torch.cat(all_GTs_moving, dim=0)  # (sum(npoints), )
                GTs_movable = torch.cat(all_GTs_movable, dim=0)  # (sum(npoints), )

                pred_moving = moving.argmax(dim=0)  # (sum(ith-#points), )
                pred_movable = movable.argmax(dim=0)  # (sum(ith-#points), )

                batch_moving_preds = list(torch.split(pred_moving, npoints.tolist()))
                batch_movable_preds = list(torch.split(pred_movable, npoints.tolist()))

                folder_names = {0: "moving", 1: "movable"}


                bin_path = os.path.join("/home/workspace/KITTI/dataset/sequences", path_seq[0], "velodyne", f"{path_name[0][:6]}.bin")
                points = np.fromfile(bin_path, dtype=np.float32).reshape(-1, 4)[:, :3]
                for batch_moving, batch_movable in zip(batch_moving_preds, batch_movable_preds):
                    for i, mos_pred in enumerate([batch_moving, batch_movable]):
                        pred_np = mos_pred.cpu().numpy().reshape((-1)).astype(np.int32)
                        pred_np = to_orig_fn(pred_np)
                        # pred_np[pred_np == 0] = 9 # unlabeled를 static으로 해야하는가?
                        if i == 0:
                            path = os.path.join(self.outputdir, "sequences", path_seq[0], "predictions", path_name[0][:6] + ".label")
                        else:
                            path = os.path.join(self.outputdir, "sequences", path_seq[0], f"predictions_{folder_names[i]}", path_name[0][:6] + ".label")
                        pred_np.tofile(path)
                        # o3d_visualize(points, pred_np)

        if cnn:  # 리스트가 비어 있지 않은 경우만 계산
            avg = sum(cnn) / len(cnn)  # 평균 계산
            avg_ms = avg * 1000  # 초 -> 밀리초 변환

            # 소수점 4자리까지 출력
            print(f"평균 시간: {avg_ms:.4f} ms")


def o3d_visualize(points, labels):
    xyz = points
    pcd = o3d.geometry.PointCloud()
    pcd.points = o3d.utility.Vector3dVector(xyz)

    color_map = {0: (255, 0, 0), 9: (0, 0, 0),
                 251: (0, 255, 255)}  # 각 클래스마다 랜덤 색상
    colors = np.array([color_map[label] for label in labels])

    # Open3D에 색상 적용
    pcd.colors = o3d.utility.Vector3dVector(colors)

    # 시각화
    o3d.visualization.draw_geometries([pcd])

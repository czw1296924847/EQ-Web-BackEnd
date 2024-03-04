from rest_framework.exceptions import APIException
from rest_framework.viewsets import GenericViewSet
from rest_framework.mixins import RetrieveModelMixin, ListModelMixin, CreateModelMixin, UpdateModelMixin
from rest_framework import views, status, generics
from rest_framework.response import Response
from django.db import transaction
from django.shortcuts import render
from celery import shared_task
import json
import pandas as pd
import numpy as np
import subprocess
import re
import os
import os.path as osp
from .serializers import *
from .models import *
from func.process import ROOT, RE_AD, DEFAULT_MODELS, DEFAULT_LIBS, PY_AD
from func.process import get_dist, get_lib_by_files, duplicate_lib, is_error
from func.net import cal_metrics


def get_model_by_pk(pk):
    """
    get DlModel object by primary key
    """
    try:
        model = DlModel.objects.get(pk=pk)
    except DlModel.DoesNotExist:
        return Response(status=status.HTTP_404_NOT_FOUND)
    return model


def get_model_by_name(name):
    """
    get DlModel object by 'name'
    """
    try:
        model = DlModel.objects.get(name=name)
    except DlModel.DoesNotExist:
        return Response(status=status.HTTP_404_NOT_FOUND)
    return model


def get_record(opt):
    """
    get existing records generated by 'ModelTrainView' and 'ModelTestView'
    """
    model_names = list(DlModel.objects.values_list('name', flat=True))  # Model Name
    record = []
    for model_name in model_names:
        path = osp.join(RE_AD, model_name)
        data_sizes = [f for f in os.listdir(path) if osp.isdir(osp.join(path, f))]  # Data Size
        record_one_model = []
        for data_size in data_sizes:
            files = os.listdir(osp.join(path, str(data_size)))  # File name
            for file in files:
                params = file.split('_')
                if (params[0] != opt) or (params[1] != 'true'):
                    continue
                sm_scale, chunk_name, data_size_train = params[2], params[3], int(params[4])
                train_ratio = np.round(data_size_train / int(data_size), 2)
                record_one_file = {
                    "train_ratio": str(train_ratio),
                    "data_size": str(data_size),
                    "sm_scale": sm_scale,
                    "chunk_name": chunk_name
                }
                record_one_model.append(record_one_file)
        record.append({'model_name': model_name, 'record': record_one_model})
    return record


class RunView(views.APIView):
    def __init__(self):
        super().__init__()
        self.model = None

    def concat_code(self, depends):
        code = ""
        for depend in depends:
            code_ = getattr(self.model, depend)
            code = code + "\n\n" + code_
        return code

    def post(self, request):
        """
        Run Python code

        :param request:
        :return:
        """
        name = request.data['name']
        depends = request.data['depends']

        if not name or not depends:
            return Response("Missing 'name' or 'depends' in request data", status=status.HTTP_404_NOT_FOUND)

        self.model = get_model_by_name(name)
        code = self.concat_code(depends)

        # Generate Python Code
        code_path = 'run.py'
        with open(code_path, 'w', encoding='utf-8') as code_file:
            code_file.write(code)
        # print("代码：", code)

        try:
            command = [PY_AD, code_path]
            # print("指令：{}\n".format(" ".join(command)))
            result = subprocess.run(command, capture_output=True, text=True)
            print("输出：\n{}\n".format(result.stdout))
            print("错误：\n{}\n".format(result.stderr))
            if len(result.stderr) > 0:
                if is_error(result.stderr):
                    return Response(result.stderr, status=status.HTTP_502_BAD_GATEWAY)
            return Response(result.stdout, status=status.HTTP_200_OK)
        except subprocess.CalledProcessError as e:
            return Response(e.output.decode('utf-8').strip(), status=status.HTTP_502_BAD_GATEWAY)
        finally:
            # print("process: ", self.model_status.process)
            if osp.exists(code_path):
                os.remove(code_path)

class ModelTrainView(views.APIView):
    def get(self, request, model_name):
        """
        Get trained model information

        :param model_name:
        :param request:
        :return:
        """
        return Response(get_record("train"))

    def post(self, request, model_name):
        """
        Train model, from TrainParam.js

        :param request:
        :param model_name: Model name, like: MagInfoNet, EQGraphNet, MagNet,
        :return: Train result, given by network.cal_metrics
        """
        from web.wsgi import registry
        model = DlModel.objects.filter(name=model_name)[0]

        if model.situation == "testing":
            return Response({"error": "Is testing"}, status=status.HTTP_409_CONFLICT)
        model.situation = "training"
        model.save()

        print(registry.models)
        model_object = registry.models[model.id]
        if model_name in DEFAULT_MODELS:
            result_train = model_object.training(request.data, model_name)
        else:
            result_train = ""
            print("model_object: ", model_object)
            print("result_train: ", result_train)
            return Response(status=status.HTTP_400_BAD_REQUEST)
        model.situation = "Free"
        model.save()
        return Response(result_train)


class ModelTestView(views.APIView):
    def get(self, request, model_name):
        """
        Get tested model information

        :param model_name:
        :param request:
        :return:
        """
        return Response(get_record("test"))

    def post(self, request, model_name):
        """
        Test model, from TestParam.js

        :param request:
        :param model_name: Model name, like: MagInfoNet, EQGraphNet, MagNet,
        :return: Test result, given by network.cal_metrics
        """
        from web.wsgi import registry
        model = DlModel.objects.filter(name=model_name)[0]
        if model.situation == "training":
            return Response({"error": "Is training"}, status=status.HTTP_409_CONFLICT)
        model.situation = "testing"
        model.save()

        model_object = registry.models[model.id]
        try:
            result_test = model_object.testing(request.data, model_name)
            model.situation = "Free"
            model.save()
            return Response(result_test)
        except FileNotFoundError:
            model.situation = "Free"
            model.save()
            return Response({"error": "File not found"}, status=status.HTTP_404_NOT_FOUND)


class ModelListView(views.APIView):
    def get(self, request):
        """
        show all models, from ModelList.js

        :param request:
        :return:
        """
        model_list = DlModel.objects.all()
        serializer = DlModelSerializer(model_list, many=True, context={'request': request})
        return Response(serializer.data)

    def post(self, request):
        """
        create new model and its model_status, from ModelList.js

        :param request:
        :return:
        """
        from web.wsgi import registry
        serializer = DlModelSerializer(data=request.data)
        model_name = request.data['name']
        if DlModel.objects.filter(name=model_name):
            return Response("Model name already exists", status=status.HTTP_409_CONFLICT)
        if serializer.is_valid():
            model = serializer.save()
            if model_name not in DEFAULT_MODELS:
                DlModelStatus.objects.create(name=model_name, process="")
                registry.models[model.id] = ""
                # print(registry.models)
            return Response(serializer.data, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class FeatureListView(views.APIView):
    def get(self, request):
        """
        show all params from data set, from FeatureList.js

        :param request:
        :return:
        """
        features = Feature.objects.all()
        serializer = FeatureSerializer(features, many=True, context={'request': request})
        return Response(serializer.data)


class FeatureDistView(views.APIView):
    def get(self, request):
        """
        get value dist of feature, from FeatureDist.js

        :param request:
        :return:
        """
        v_min, v_max = None, None
        feature = request.GET.get('feature')
        bins = int(request.GET.get('bins'))
        chunk_name = request.GET.get('chunk_name')
        data_size = int(request.GET.get('data_size'))

        if feature == "source_depth_km":
            v_min, v_max = 0, 150
        elif feature == "source_magnitude":
            v_min = 0

        x, y = get_dist(feature, bins, chunk_name, data_size, v_min, v_max)

        if feature in ["source_distance_km", "source_depth_km", "snr_db",
                       "p_arrival_sample", "s_arrival_sample"]:
            x = np.round(x)
        elif feature == "source_magnitude":
            x = np.round(x, 2)

        points = [{"x": i, "y": j} for i, j in zip(x, y)]
        serializer = PointSerializer(points, many=True)
        return Response(serializer.data)


class FeatureLocateView(views.APIView):
    def get(self, request):
        """
        get earthquake source longitude and latitude, from LocateModal.js
        """
        chunk_name = request.GET.get('chunk_name')
        lo_min = float(request.GET.get('lo_min'))
        lo_max = float(request.GET.get('lo_max'))
        la_min = float(request.GET.get('la_min'))
        la_max = float(request.GET.get('la_max'))
        df = pd.read_csv(osp.join(ROOT, chunk_name, chunk_name + ".csv"))

        la = df.loc[:, "source_latitude"].values.reshape(-1)
        lo = df.loc[:, "source_longitude"].values.reshape(-1)
        sm = df.loc[:, "source_magnitude"].values.reshape(-1)

        idx = np.argwhere((la >= la_min) & (la <= la_max) & (lo >= lo_min) & (lo <= lo_max)).reshape(-1)
        la, lo, sm = la[idx], lo[idx], sm[idx]

        num = 20000
        idx_sample = np.random.choice(idx.shape[0], num, replace=False)
        la, lo, sm = la[idx_sample], lo[idx_sample], sm[idx_sample]

        sources = [{"Longitude": i, "Latitude": j, "Magnitude": k} for i, j, k in zip(lo, la, sm)]
        serializer = SourceSerializer(sources, many=True)
        return Response(serializer.data)


class ModelOptView(views.APIView):
    def put(self, request, pk):
        """
        Modify model information, from ModelList.js

        :param request:
        :param pk: Primary Key
        :return:
        """
        model = get_model_by_pk(pk)
        style = request.data['style']
        key = request.data['key']

        if style == "edit":
            if key == "library":        # add default library
                print("去重前：", request.data['value'][key])
                request.data['value'][key] = duplicate_lib(DEFAULT_LIBS + request.data['value'][key])
                print("去重后：", request.data['value'][key])

            serializer = DlModelSerializer(model, data=request.data['value'], context={'request': request})
            if serializer.is_valid():

                if key == "name":       # The corresponding DlModelStatus also modify 'name'
                    model_status = DlModelStatus.objects.get(pk=request.data['value']['pk'])
                    model_status.name = request.data['value']['name']
                    model_status.save()

                serializer.save()
                return Response(status=status.HTTP_204_NO_CONTENT)
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        elif style == "upload":
            file = request.data['file']
            content = file.read().decode('utf-8')
            if hasattr(model, key):
                if key == "library":    # remain codes like "import numpy as np"
                    content = get_lib_by_files(content, True)
                elif key in ['code_data', 'code_model', 'code_train', 'code_test']:
                    content = get_lib_by_files(content, False)
                else:
                    raise TypeError(
                        "Unknown type of 'key', must be 'library', 'code_data', 'code_model', 'code_train', 'code_test'")
                content = "\n".join(content)
                setattr(model, key, content)
                model.save()
                return Response(content, status=status.HTTP_200_OK)
            else:
                raise TypeError("'{}' is not in Model".format(key))
        else:
            raise TypeError("Unknown 'style', must be 'edit' or 'upload'!")

    def delete(self, request, pk):
        """
        Delete model information, without used (danger)

        :param request:
        :param pk: Primary Key
        :return:
        """
        default_models = ["MagInfoNet", "EQGraphNet", "MagNet", "CREIME", "ConvNetQuakeINGV"]
        model = get_model_by_pk(pk)
        if model.name in default_models:
            return Response("Cannot delete default model", status=status.HTTP_403_FORBIDDEN)
        model.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class ModelDetailView(views.APIView):
    def get(self, request, model_name):
        """
        show detail information of a specific model, from ModelDetail.js

        :param request:
        :param model_name: Model name, like: MagInfoNet, EQGraphNet, MagNet,
        :return: Model information, given by models.DlModel
        """
        model = DlModel.objects.filter(name=model_name)
        serializer = DlModelSerializer(model, many=True, context={'request': request})
        return Response(serializer.data, status=status.HTTP_200_OK)


class ModelProcessView(views.APIView):
    def get(self, request, model_name):
        """
        get the situation during train/test process
        """
        # print(DlModelStatus.objects)
        process = DlModelStatus.objects.values_list('process', flat=True).get(name=model_name)
        return Response(process, status=status.HTTP_200_OK)

    def put(self, request, model_name):
        """
        initialize the model process, to be ""
        """
        model = DlModelStatus.objects.get(name=model_name)
        model.process = ""
        model.save()
        return Response(status=status.HTTP_200_OK)


def get_params(request):
    """
    Read params, from OptResult.js
    """
    train_ratio = float(request.GET.get('train_ratio'))
    data_size = int(request.GET.get('data_size'))
    sm_scale = request.GET.get('sm_scale')
    chunk_name = request.GET.get('chunk_name')
    data_size_train = int(data_size * train_ratio)
    data_size_test = data_size - data_size_train
    return sm_scale, chunk_name, data_size, data_size_train, data_size_test


def get_result_ad(re_ad, opt, sm_scale, chunk_name, data_size_train, data_size_test):
    """
    get result file address
    """
    true_ad = osp.join(re_ad, "{}_true_{}_{}_{}_{}.npy".
                       format(opt, sm_scale, chunk_name, str(data_size_train), str(data_size_test)))
    pred_ad = osp.join(re_ad, "{}_pred_{}_{}_{}_{}.npy".
                       format(opt, sm_scale, chunk_name, str(data_size_train), str(data_size_test)))
    loss_ad = osp.join(re_ad, "{}_loss_{}_{}_{}_{}.npy".
                       format(opt, sm_scale, chunk_name, str(data_size_train), str(data_size_test)))
    model_ad = osp.join(re_ad, "model_{}_{}_{}_{}.pkl".
                        format(sm_scale, chunk_name, str(data_size_train), str(data_size_test)))
    return true_ad, pred_ad, loss_ad, model_ad


def remain_range(true, pred, v_min, v_max):
    """
    remain data in given [v_min, v_max]
    """
    idx = np.argwhere((true >= v_min) & (true <= v_max) & (pred >= v_min) & (pred <= v_max)).reshape(-1)
    true, pred = true[idx], pred[idx]
    return true, pred


class CompTruePredView(views.APIView):
    def get(self, request, model_name, opt, *args, **kwargs):
        """
        Compare the true and predicted magnitudes, from OptResult.js

        :param request:
        :param model_name: Model name
        :param opt: 'train' or 'test'
        :return: True magnitudes or Predicted operation
        """
        sm_scale, chunk_name, data_size, data_size_train, data_size_test = get_params(request)
        re_ad = osp.join(RE_AD, model_name, str(data_size))

        # set a smaller number for web show, to avoid web crash
        num_show = 10000
        true_ad, pred_ad, _, _ = get_result_ad(re_ad, opt, sm_scale, chunk_name, data_size_train, data_size_test)
        true, pred = np.load(true_ad), np.load(pred_ad)

        v_min, v_max = 0, 3.5
        true, pred = remain_range(true, pred, v_min, v_max)

        points = [{"x": i, "y": j} for i, j in zip(true[:num_show], pred[:num_show])]
        r2, rmse, e_mean, e_std = cal_metrics(true, pred)
        data = {
            'points': points,
            'r2': str(np.round(float(r2), 4)),
            'rmse': str(np.round(float(rmse), 4)),
            'e_mean': str(np.round(float(e_mean), 4)),
            'e_std': str(np.round(float(e_std), 4)),
        }
        serializer = ResultSerializer(data)
        return Response(serializer.data)


class LossCurveView(views.APIView):
    def get(self, request, model_name, opt, *args, **kwargs):
        """
        Plot the loss curve during training, from OptResult.js

        :param request:
        :param model_name: Model name
        :param opt: 'train' or 'test'
        :return:
        """
        sm_scale, chunk_name, data_size, data_size_train, data_size_test = get_params(request)
        re_ad = osp.join(RE_AD, model_name, str(data_size))
        loss = np.load(osp.join(re_ad, "{}_loss_{}_{}_{}_{}.npy".
                                format(opt, sm_scale, chunk_name, str(data_size_train), str(data_size_test))))
        points = [{"x": i, "y": j} for i, j in zip(np.arange(loss.shape[0]), loss)]
        serializer = PointSerializer(points, many=True)
        return Response(serializer.data)


class ModelRecordView(views.APIView):
    def get(self, request, model_name, opt, *args, **kwargs):
        """
        get all training or testing records, from OptRecord.js
        """
        sm_scale, chunk_name, data_size, data_size_train, data_size_test = get_params(request)
        re_ad = osp.join(RE_AD, model_name, str(data_size))
        true_ad, pred_ad, loss_ad, model_ad = get_result_ad(re_ad, opt, sm_scale, chunk_name, data_size_train,
                                                            data_size_test)
        if opt == "train":
            return Response(
                osp.exists(true_ad) and osp.exists(pred_ad) and osp.exists(loss_ad) and osp.exists(model_ad),
                status=status.HTTP_200_OK)
        elif opt == "test":
            return Response(osp.exists(true_ad) and osp.exists(pred_ad) and osp.exists(loss_ad),
                            status=status.HTTP_200_OK)
        else:
            return Response(False, status=status.HTTP_400_BAD_REQUEST)

    def delete(self, request, model_name, opt, *args, **kwargs):
        """
        Delete the train/test result (model.pkl, loss.npy, true.npy, pred.npy)
        """
        sm_scale, chunk_name, data_size, data_size_train, data_size_test = get_params(request)
        re_ad = osp.join(RE_AD, model_name, str(data_size))
        true_ad, pred_ad, loss_ad, model_ad = get_result_ad(re_ad, opt, sm_scale, chunk_name,
                                                            data_size_train, data_size_test)
        os.remove(true_ad), os.remove(pred_ad), os.remove(loss_ad)
        if opt == "train":
            os.remove(model_ad)
        return Response(status=status.HTTP_204_NO_CONTENT)


class LoginView(views.APIView):
    def get(self, request, *args, **kwargs):
        """
        check if user can log in web (given in wsgi.py)
        """
        username = request.GET.get('username')
        password = request.GET.get('password')
        try:
            user = User.objects.get(username=username)
        except User.DoesNotExist:
            print("username: {}, password: {}, 用户不存在".format(username, password))
            return Response({"msg": "user_not_exist"}, status=status.HTTP_404_NOT_FOUND)
        if user.password == password:
            print("username: {}, password: {}, 登录成功".format(username, password))
            return Response({"msg": "login_success"}, status=status.HTTP_200_OK)
        else:
            print("username: {}, password: {}, 密码错误".format(username, password))
            return Response({"msg": "password_error"}, status=status.HTTP_401_UNAUTHORIZED)


"""
Practice Code
"""


def index(request):
    return render(request, 'index.html', {})


def room(request, room_name):
    return render(request, 'room.html', {
        'room_name': room_name
    })

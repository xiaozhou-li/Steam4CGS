import requests
import pandas as pd
import numpy as np
import pprint
from urllib import *
import json, csv
from datetime import *
import glob

desired_width=320
pd.set_option('display.width', desired_width)
np.set_printoptions(linewidth=desired_width)
pd.set_option('display.max_columns',25)

pp = pprint.PrettyPrinter()

features = ['recommendationid', 'review', 'timestamp_created', 'timestamp_updated', 'voted_up', 'votes_up', 'votes_funny', 'comment_count', 'author_steamid', 'author_num_games_owned', 'author_num_reviews', 'author_playtime_forever', 'author_playtime_last_two_weeks', 'author_last_played']

#steamapi.core.APIConnection(api_key="6FCFCE5E8A5859EFC0E182056B4E612E", validate_key=True)  # <-- Insert API key here
#me = steamapi.user.SteamUser(userurl="sideshowli")
#print(me.last_logoff)

testing = requests.get('https://store.steampowered.com/appreviews/1191630?json=1&filter=all&cursor=*').json()
testing2 = requests.get('https://store.steampowered.com/appreviews/1191630?json=1&filter=all&cursor={}'.format(testing['cursor'])).json()

def plusto2b(thecursor):
    if '+' in thecursor:
        return '%2B'.join(thecursor.split('+'))
    else:
        return thecursor

def getreviews(gameid, jsonfilename):
    gameurl = 'https://store.steampowered.com/appreviews/{}?json=1&language=english&filter=recent&purchase_type=all&num_per_page=100&cursor='.format(gameid)
    currentCursor = '*'
    cursorlist = []
    reviewdata = []
    while(1==1):
        try:
            reviewsjson = requests.get(gameurl+plusto2b(currentCursor)).json()
        except:
            break
        currentCursor = reviewsjson['cursor']
        if currentCursor in cursorlist:
            break
        else:
            cursorlist.append(currentCursor)
            reviewdata.extend(reviewsjson['reviews'])
            print(len(reviewdata))
    print(len(reviewdata))
    with open(jsonfilename, 'w') as fout:
        json.dump(reviewdata, fout)

def getreviewsAndAutoName(gameid):
    gameurl = 'https://store.steampowered.com/appreviews/{}?json=1&language=english&filter=recent&purchase_type=all&num_per_page=100&cursor='.format(gameid)
    currentCursor = '*'
    cursorlist = []
    reviewdata = []
    while(1==1):
        try:
            reviewsjson = requests.get(gameurl+plusto2b(currentCursor)).json()
        except:
            break
        currentCursor = reviewsjson['cursor']
        if currentCursor in cursorlist:
            break
        else:
            cursorlist.append(currentCursor)
            reviewdata.extend(reviewsjson['reviews'])
            print(len(reviewdata))
    print(len(reviewdata))
    with open(gameid+'_reviews.json', 'w') as fout:
        json.dump(reviewdata, fout)

def viewreviewjson(filename):
    with open(filename, "r") as read_file:
        data = json.load(read_file)
    print(len(list(set([x['recommendationid'] for x in data]))))

def reviewItemDictReady(reviewitem):
    author = reviewitem.pop('author')
    author = dict(zip(['author_'+x for x in author.keys()], author.values()))
    reviewitem.update(author)
    return reviewitem

def writeJsonReviewData2ReviewLevelCSV(jsonfilename, csvfilename):
    with open(jsonfilename, 'r') as jsonreadfile:
        jsondata = json.load(jsonreadfile)
    with open(csvfilename, 'a') as csvfile:
        writer = csv.writer(csvfile, delimiter=',')
        writer.writerow(features)
    for i in range(len(jsondata)):
        dictitem = reviewItemDictReady(jsondata[i])
        dictitem['author_last_played'] = datetime.utcfromtimestamp(dictitem['author_last_played']).strftime('%Y-%m-%d %H:%M:%S')
        dictitem['timestamp_created'] = datetime.utcfromtimestamp(dictitem['timestamp_created']).strftime('%Y-%m-%d %H:%M:%S')
        dictitem['timestamp_updated'] = datetime.utcfromtimestamp(dictitem['timestamp_updated']).strftime('%Y-%m-%d %H:%M:%S')
        try:
            with open(csvfilename, 'a') as csvfile:
                writer = csv.writer(csvfile, delimiter=',')
                writer.writerow([dictitem[x] for x in features])
            print("No. {} review written!".format(i+1))
        except:
            print("No. {} review ERROR!".format(i+1))
            break



#getreviews('275850', 'review_api_275850_20240318.json')
#viewreviewjson('review_api_275850_20210114.json')
#writeJsonReviewData2ReviewLevelCSV('review_api_275850_20240318.json', 'nomanskydata20240318.csv')

df = pd.read_csv('steamproducts2020.csv')
print(df.shape)
#thegamelist = df.loc[:, 'id'].values.tolist()
#print(thegamelist)

#for game in thegamelist[13:]:
#    print(game)
#    getreviewsAndAutoName(str(game))
#    writeJsonReviewData2ReviewLevelCSV(str(game)+'_reviews.json', str(game)+'_reviews.csv')

def getCommentsByEachAuthorID(typefolder, newcommentstablename):
    all_commit_files = glob.glob(typefolder+"/*_reviews.csv")
    df_all = pd.concat((pd.read_csv(f) for f in all_commit_files), ignore_index=True)
    print(df_all.head())
    df_all_g = df_all.groupby('author_steamid').count()
    df_all_g.reset_index(inplace=True)
    print(df_all_g)
    auhtorlist = df_all_g.loc[:,'author_steamid'].values.tolist()
    with open(newcommentstablename, 'a', encoding='utf-8') as csvfile:
        writer = csv.writer(csvfile, delimiter=',')
        writer.writerow(['author_steamid', 'review'])
    for author in auhtorlist:
        print(author)
        thecommentlist = df_all.loc[df_all['author_steamid'] == author, 'review'].values.tolist()
        thecommentlist = [str(x) for x in thecommentlist]
        with open(newcommentstablename, 'a', encoding='utf-8') as csvfile:
            writer = csv.writer(csvfile, delimiter=',')
            writer.writerow([author, ' '.join(thecommentlist)])


#getreviewsAndAutoName(str(207140))
#writeJsonReviewData2ReviewLevelCSV('207140_reviews.json', '207140_reviews.csv')

#getCommentsByEachAuthorID('action', 'action_reviewsByAuthorID.csv')

#df = pd.read_csv("steamproducts2020.csv")
#df = df.loc[df['genres']=="['Action', 'Adventure']", :]
#print(df.head())
#print(df.shape)
#df.to_csv("steamproducts2020_action_adventure.csv", index=False)

df = pd.read_csv('steamproducts2020_action_adventure_filteredPos80.csv')
#df = df.loc[((df['sentiment']=="Overwhelmingly Positive") | (df['sentiment']=="Very Positive") | (df['sentiment']=="Mostly Positive")) & (df['metascore'] > 80)]
#print(df.shape)
print(df)
#df.to_csv("steamproducts2020_action_adventure_filteredPos80.csv", index=False)

#gameidlist = df['id'].values.tolist()


#getreviews(45740, "45740_reviews.json")
#writeJsonReviewData2ReviewLevelCSV('271590_reviews.json', '271590_reviews.csv')
